import asyncio
from io import BytesIO
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
from contextlib import asynccontextmanager

import aiofiles
from PIL import Image
from bs4 import Tag, BeautifulSoup
from starlette.requests import Request
from fastapi.staticfiles import StaticFiles
from fastapi import FastAPI, UploadFile, BackgroundTasks
from fastapi.responses import Response, StreamingResponse

from gsuid_core.sv import SL
from gsuid_core.gss import gss
import gsuid_core.global_val as gv
from gsuid_core.data_store import image_res
from gsuid_core.webconsole.mount_app import site
from gsuid_core.segment import Message, MessageSegment
from gsuid_core.config import CONFIG_DEFAULT, core_config
from gsuid_core.aps import start_scheduler, shutdown_scheduler
from gsuid_core.server import core_start_def, core_shutdown_def
from gsuid_core.utils.database.models import CoreUser, CoreGroup
from gsuid_core.utils.plugins_config.models import (
    GsImageConfig,
    GsListStrConfig,
)
from gsuid_core.utils.plugins_config.gs_config import (
    all_config_list,
    core_plugins_config,
)
from gsuid_core.logger import (
    LOG_PATH,
    HistoryLogData,
    logger,
    read_log,
    clean_log,
)
from gsuid_core.utils.plugins_update._plugins import (
    check_status,
    check_plugins,
    install_plugin,
    update_plugins,
    check_can_update,
    get_plugins_list,
)

is_clean_pic = core_plugins_config.get_config('EnableCleanPicSrv').data
pic_expire_time = core_plugins_config.get_config('ScheduledCleanPicSrv').data


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        logger.info(
            '♻ [GsCore] 执行启动Hook函数中！',
            [_def.__name__ for _def in core_start_def],
        )
        for _def in core_start_def:
            if asyncio.iscoroutinefunction(_def):
                asyncio.create_task(_def())
            else:
                asyncio.create_task(asyncio.to_thread(_def))

    except Exception as e:
        logger.exception(e)

    from gsuid_core.webconsole.__init__ import start_check

    await start_check()  # type:ignore
    await start_scheduler()
    asyncio.create_task(clean_log())

    yield

    await shutdown_scheduler()

    try:
        logger.info(
            '[GsCore] 执行关闭Hook函数中！',
            [_def.__name__ for _def in core_shutdown_def],
        )
        _task = [_def() for _def in core_shutdown_def]
        await asyncio.gather(*_task)
    except Exception as e:
        logger.exception(e)


app = FastAPI(lifespan=lifespan)


@app.post('/genshinuid/uploadImage/{suffix}/{filename}/{UPLOAD_PATH:path}')
@site.auth.requires('root', response=Response(status_code=403))
async def _upload_image(
    request: Request,
    UPLOAD_PATH: str,
    file: UploadFile,
    filename: Optional[str],
    suffix: Optional[str],
):
    path = Path(UPLOAD_PATH)
    # 利用uuid保存图片
    file_name = file.filename
    if not filename:
        if file_name:
            file_name = file_name.split('.')[-1]
            file_name = (
                f'{datetime.now().strftime("%Y%m%d%H%M%S")}.{file_name}'
            )
        else:
            file_name = 'image.jpg'
    else:
        if suffix:
            file_name = f'{filename}.{suffix}'
        else:
            file_name = f'{filename}.jpg'

    file_path = path / file_name
    if not file_path.parent.exists():
        file_path.parent.mkdir(parents=True)
    async with aiofiles.open(file_path, 'wb') as f:
        content = await file.read()
        await f.write(content)


@app.get('/genshinuid/getImage/{suffix}/{filename}/{IMAGE_PATH:path}')
@site.auth.requires('root', response=Response(status_code=403))
async def _get_image(
    request: Request,
    IMAGE_PATH: str,
    filename: str,
    suffix: str = 'str',
):
    path = Path(IMAGE_PATH)
    file_path = path / f'{filename}.{suffix}'
    if not file_path.exists():
        return Response(status_code=404)

    # 返回URL
    return Response(
        content=file_path.read_bytes(),
        media_type='image/jpeg',
    )


@app.post('/genshinuid/setSV/{name}')
@site.auth.requires('root')
def _set_SV(request: Request, data: Dict, name: str):
    if name in SL.lst:
        sv = SL.lst[name]
        data['pm'] = int(data['pm'])
        data['priority'] = int(data['priority'])

        data['black_list'] = data['black_list'].split(',')
        data['white_list'] = data['white_list'].split(',')

        if data['black_list'] == ['']:
            data['black_list'] = []
        if data['white_list'] == ['']:
            data['white_list'] = []
        sv.set(False, **data)


@app.post('/genshinuid/setPlugins/{name}')
@site.auth.requires('root')
def _set_Plugins(request: Request, data: Dict, name: str):
    if name in SL.plugins:
        plugin = SL.plugins[name]
        data['pm'] = int(data['pm'])
        data['priority'] = int(data['priority'])
        if 'prefix' in data:
            data['prefix'] = data['prefix'].split(',')

        data['black_list'] = data['black_list'].split(',')
        data['white_list'] = data['white_list'].split(',')

        if data['black_list'] == ['']:
            data['black_list'] = []
        if data['white_list'] == ['']:
            data['white_list'] = []
        plugin.set(False, **data)


@app.post('/genshinuid/setGsConfig/{config_name}')
@site.auth.requires('root')
def _set_Config(request: Request, data: Dict, config_name: str):
    for name in data:
        if name == 'params':
            continue
        cc = all_config_list[config_name]
        config = cc[name]
        if isinstance(config, GsListStrConfig):
            data[name] = data[name].replace('：', ':').replace(':', ',')
            value = data[name].split(',')
        elif isinstance(config, GsImageConfig):
            value: dict = cc.config_default[name].data  # type: ignore
        else:
            value = data[name]
        cc.set_config(name, value)
    return {"status": 0, "msg": "成功！"}


@app.post('/genshinuid/setCoreConfig')
@site.auth.requires('root')
def _set_Core_Config(request: Request, data: Dict):
    result = {}
    for i in data:
        if (
            i in CONFIG_DEFAULT and isinstance(CONFIG_DEFAULT[i], List)
        ) or i in ['log_output']:
            v = data[i].split(',')
        else:
            v = data[i]

        if i in ['log_level', 'log_output']:
            g = i.split('_')
            k = g[0]
            if k not in result:
                result[k] = {}
            result[k][g[1]] = v
            continue

        core_config.set_config(i, v)

    for r in result:
        core_config.set_config(r, result[r])

    return {"status": 0, "msg": "成功！"}


@app.get('/genshinuid/api/getPlugins')
@site.auth.requires('root')
async def _get_plugins(request: Request):
    tasks = []
    plugins_list = await get_plugins_list()
    for name in plugins_list:
        plugin = plugins_list[name]
        link = plugin['link']
        plugin_name = link.split('/')[-1]
        sample = {
            'label': plugin_name,
            'key': name,
            'status': await check_status(plugin_name),
            'remark': plugin['info'],
        }
        tasks.append(sample)

    return tasks


@app.get('/genshinuid/api/getAnalysisData/{bot_id}/{bot_self_id}')
@site.auth.requires('root')
async def _get_data_analysis(
    request: Request,
    bot_id: str,
    bot_self_id: str,
):
    if bot_id not in gv.bot_val:
        retcode, msg, data = -1, '不存在该bot_id!', {}
    elif bot_self_id not in gv.bot_val[bot_id]:
        retcode, msg, data = -1, '不存在该bot_self_id!', {}
    else:
        retcode, msg = 0, 'ok'

        xaxis = []
        series = []

        send_data = []
        receive_data = []
        command_data = []
        image_gen_data = []

        seven_data = await gv.get_value_analysis(bot_id, bot_self_id)
        for day in seven_data:
            xaxis.append(day)
            local_val = seven_data[day]
            send_data.append(local_val['send'])
            receive_data.append(local_val['receive'])
            command_data.append(local_val['command'])
            image_gen_data.append(local_val['image'])

        series.append({'name': '发送', 'type': 'line', 'data': send_data})
        series.append(
            {
                'name': '接收',
                'type': 'line',
                'data': receive_data,
            }
        )
        series.append(
            {
                'name': '命令调用',
                'type': 'line',
                'data': command_data,
            }
        )
        series.append(
            {
                'name': '图片生成',
                'type': 'line',
                'data': image_gen_data,
            }
        )

        data = {
            'title': {'text': ''},
            'tooltip': {'trigger': 'axis'},
            'legend': {'data': ['发送', '接收', '命令调用', '图片生成']},
            'xAxis': {'type': 'category', 'boundaryGap': False, 'data': xaxis},
            'yAxis': {'type': 'value'},
            'series': series,
        }

    return {'status': retcode, 'msg': msg, 'data': data}


@app.get('/genshinuid/api/getAnalysisUserGroup/{bot_id}/{bot_self_id}')
@site.auth.requires('root')
async def _get_usergroup_analysis(
    request: Request,
    bot_id: str,
    bot_self_id: str,
):
    if bot_id not in gv.bot_val:
        retcode, msg, data = -1, '不存在该bot_id!', {}
    elif bot_self_id not in gv.bot_val[bot_id]:
        retcode, msg, data = -1, '不存在该bot_self_id!', {}
    else:
        retcode, msg = 0, 'ok'

        xaxis = []
        series = []

        group_data = []
        user_data = []

        seven_data = await gv.get_value_analysis(bot_id, bot_self_id)
        for day in seven_data:
            xaxis.append(day)
            local_val = seven_data[day]
            group_data.append(len(local_val['group']))
            user_data.append(len(local_val['user']))

        series.append({'name': '用户', 'type': 'bar', 'data': user_data})
        series.append(
            {
                'name': '群组',
                'type': 'bar',
                'data': group_data,
            }
        )

        data = {
            'title': {'text': ''},
            'tooltip': {'trigger': 'axis'},
            'legend': {'data': ['用户', '群组']},
            'xAxis': {'type': 'category', 'boundaryGap': False, 'data': xaxis},
            'yAxis': {'type': 'value'},
            'series': series,
        }

    return {'status': retcode, 'msg': msg, 'data': data}


@app.post('/genshinuid/api/updatePlugins')
@site.auth.requires('root')
async def _update_plugins(request: Request, data: Dict):
    repo = check_plugins(data['label'])
    if repo:
        if check_can_update(repo):
            try:
                await update_plugins(data['label'])
                retcode = 0
            except:  # noqa:E722
                retcode = -1
        else:
            retcode = 0
    else:
        try:
            retcode = await install_plugin(data['key'])
            retcode = 0
        except:  # noqa:E722
            retcode = -1
    return {'status': retcode, 'msg': '', 'data': {}}


@app.post('/genshinuid/api/BatchPush')
@site.auth.requires('root')
async def _batch_push(request: Request, data: Dict):
    send_msg = data['push_text']
    soup = BeautifulSoup(send_msg, 'lxml')

    msg: List[Message] = []
    text_list: List[Tag] = list(soup.find_all('p'))
    for text in text_list:
        msg.append(MessageSegment.text(str(text)[3:-4] + '\n'))

    img_tag: List[Tag] = list(soup.find_all('img'))
    for img in img_tag:
        src: str = img.get('src')  # type: ignore
        width: str = img.get('width')  # type: ignore
        height: str = img.get('height')  # type: ignore

        base64_data = 'base64://' + src.split(',')[-1]

        msg.append(MessageSegment.image(base64_data))
        msg.append(MessageSegment.image_size((int(width), int(height))))

    send_target: List[str] = data['push_tag'].split(',')
    push_bots: List[str] = data['push_bot'].split(',')
    user_sends: Dict[str, List[str]] = {}
    group_sends: Dict[str, List[str]] = {}

    if 'ALLUSER' in send_target:
        all_user = await CoreUser.get_all_user()
        if all_user:
            for user in all_user:
                if user.bot_id not in user_sends:
                    user_sends[user.bot_id] = [user.user_id]
                else:
                    if user.user_id not in user_sends[user.bot_id]:
                        user_sends[user.bot_id].append(user.user_id)
        send_target.remove('ALLUSER')

    if 'ALLGROUP' in send_target:
        all_group = await CoreGroup.get_all_group()
        if all_group:
            for group in all_group:
                if group.bot_id not in group_sends:
                    group_sends[group.bot_id] = [group.group_id]
                else:
                    if group.group_id not in group_sends[group.bot_id]:
                        group_sends[group.bot_id].append(group.group_id)
        send_target.remove('ALLGROUP')

    for _target in send_target:
        if '|' not in _target:
            continue
        targets = _target.split('|')
        target, bot_id = targets[0], targets[1]
        if target.startswith('g:'):
            group_id = target.split(':')[1]
            if bot_id not in group_sends:
                group_sends[bot_id] = [group_id]
            else:
                if group_id not in group_sends[bot_id]:
                    group_sends[bot_id].append(group_id)
        else:
            user_id = target.split(':')[1]
            if bot_id not in user_sends:
                user_sends[bot_id] = [user_id]
            else:
                if user_id not in user_sends[bot_id]:
                    user_sends[bot_id].append(user_id)

    s = [group_sends, user_sends]
    for BOT_ID in gss.active_bot:
        if BOT_ID not in push_bots:
            continue
        for index, sends in enumerate(s):
            send_type = 'group' if index == 0 else 'direct'
            for bot_id in sends:
                for uuid in sends[bot_id]:
                    if index == 0:
                        msg.append(Message('group', uuid))
                    await gss.active_bot[BOT_ID].target_send(
                        msg,
                        send_type,
                        uuid,
                        bot_id,
                        '',
                        '',
                    )

    return {'status': 0, 'msg': '推送成功！', 'data': '推送成功！'}


async def delete_image(image_path: Path):
    await asyncio.sleep(int(pic_expire_time))
    image_path.unlink()


app.mount(
    "/webstatic",
    StaticFiles(directory=Path(__file__).parent / 'webstatic'),
    name="static",
)


@app.head('/genshinuid/image/{image_id}')
@app.get('/genshinuid/image/{image_id}')
async def get_image(image_id: str, background_tasks: BackgroundTasks):
    path = image_res / image_id
    if not path.exists() and '.' not in image_id:
        path = image_res / f'{image_id}.jpg'

    if not path.exists():
        return Response(status_code=404)

    image = Image.open(path).convert('RGB')
    image_bytes = BytesIO()
    image.save(image_bytes, format='JPEG')
    image_bytes.seek(0)
    response = StreamingResponse(image_bytes, media_type='image/png')
    if is_clean_pic:
        asyncio.create_task(delete_image(path))
    return response


@app.get("/corelogs")
@site.auth.requires('root')
async def core_log(request: Request):
    return StreamingResponse(read_log(), media_type='text/event-stream')


@app.post('/genshinuid/api/loadData/{bot_id}/{bot_self_id}')
@site.auth.requires('root')
async def get_history_data(
    request: Request,
    data: Dict,
    bot_id: str,
    bot_self_id: str,
):
    name = data.get('name', None)
    a_pie_data = []
    b_pie_data = []
    if name is None:
        seven_day = await gv.get_value_analysis(bot_id, bot_self_id)
        local_val = seven_day[list(seven_day.keys())[0]]
    else:
        local_val = await gv.get_sp_val(bot_id, bot_self_id, name)

    c_data = {}
    for g in local_val['group']:
        a_pie_data.append(
            {"value": sum(list(local_val['group'][g].values())), "name": g}
        )
    for u in local_val['user']:
        for c in local_val['user'][u]:
            if c not in c_data:
                c_data[c] = 0
            c_data[c] += local_val['user'][u][c]

    for c in c_data:
        b_pie_data.append({"value": c_data[c], "name": c})
        # l_data.append(c)

    data = {
        "series": [
            {
                "data": a_pie_data,
                "type": "pie",
                "name": "群组触发命令",
                # "selectedMode": "single",
                "radius": [0, "30%"],
                "label": {"position": "inner", "fontSize": 14},
                "labelLine": {"show": False},
            },
            {
                "name": "命令调用次数",
                "type": "pie",
                "radius": ["45%", "60%"],
                "labelLine": {"length": 30},
                "label": {
                    "formatter": "{a|{a}}{abg|}\n{hr|}\n  {b|{b}：}{c}  {per|{d}%}",  # noqa: E501
                    "backgroundColor": "#F6F8FC",
                    "borderColor": "#8C8D8E",
                    "borderWidth": 1,
                    "borderRadius": 4,
                    "rich": {
                        "a": {
                            "color": "#6E7079",
                            "lineHeight": 22,
                            "align": "center",
                        },
                        "hr": {
                            "borderColor": "#8C8D8E",
                            "width": "100%",
                            "borderWidth": 1,
                            "height": 0,
                        },
                        "b": {
                            "color": "#4C5058",
                            "fontSize": 14,
                            "fontWeight": "bold",
                            "lineHeight": 33,
                        },
                        "per": {
                            "color": "#fff",
                            "backgroundColor": "#4C5058",
                            "padding": [3, 4],
                            "borderRadius": 4,
                        },
                    },
                },
                "data": b_pie_data,
            },
        ],
        "tooltip": {
            "trigger": "item",
            "formatter": "{a} <br/>{b}: {c} ({d}%)",
        },
    }
    return {
        'status': 0,
        'msg': 'ok',
        'data': data,
    }


@app.get('/genshinuid/api/historyLogs')
@site.auth.requires('root')
async def get_history_logs(
    request: Request,
    date: Optional[str] = None,
    page: int = 0,
    perPage: int = 0,
):
    if date is None:
        date = datetime.now().strftime('%Y-%m-%d')

    if date.endswith('.log'):
        date = date.removesuffix('.log')

    history_log_data = HistoryLogData()
    log_files = await history_log_data.get_parse_logs(LOG_PATH / f'{date}.log')
    total = len(log_files)
    if page != 0 and perPage != 0:
        start = (page - 1) * perPage
        end = start + perPage
        log_file = log_files[start:end]
    else:
        log_file = log_files
    return {
        'status': 0,
        'msg': 'ok',
        'data': {
            'count': total,
            'rows': log_file,
        },
    }
