import asyncio
import json
import os
import sys
import numpy as np
from functools import partial
import aiohttp
from aiohttp import MultipartWriter
from aiohttp import WSCloseCode
from aiohttp import web
import weakref

from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaRelay

import logging
import time

logging.basicConfig(level=logging.ERROR)
pcs = set()


async def index(request, **kwargs):
    folder = kwargs['folder']
    content = open(os.path.join(folder, "index.html"), "r").read()
    return web.Response(content_type="text/html", text=content)


async def javascript(request, **kwargs):
    folder = kwargs['folder']
    js = kwargs['js']
    content = open(os.path.join(folder, "js/%s" % js), "r").read()
    return web.Response(content_type="application/javascript", text=content)


async def mjpeg_handler(request):
    my_boundary = 'image-boundary'
    response = web.StreamResponse(
        status=200,
        reason='OK',
        headers={
                'Content-Type': 
                    'multipart/x-mixed-replace;boundary={}'.format(my_boundary)
            }
    )
    await response.prepare(request)
    image_buffer_manager = request.app['image_buffer_manager']
    while True:
        jpeg_bytes = await image_buffer_manager.get_image()
        with MultipartWriter('image/jpeg', boundary=my_boundary) as mpwriter:
            mpwriter.append(jpeg_bytes, {
                'Content-Type': 'image/jpeg'
            })
            try:
                await mpwriter.write(response, close_boundary=False)
            except ConnectionResetError :
                logging.info("Client connection closed")
                break
        await response.write(b"\r\n")


async def offer(request, **kwargs):
    video = kwargs['video']
    if("broadcast" in kwargs and kwargs["broadcast"]):
        video = MediaRelay().subscribe(video)

    params = await request.json()

    offer = RTCSessionDescription(
        sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection()
    pcs.add(pc)

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        print("Connection state is %s" % pc.connectionState)
        if pc.connectionState == "failed":
            await pc.close()
            pcs.discard(pc)

    # open media source
    audio = None

    await pc.setRemoteDescription(offer)
    for t in pc.getTransceivers():
        if t.kind == "audio" and audio:
            pc.addTrack(audio)
        elif t.kind == "video" and video:
            pc.addTrack(video)

    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return web.Response(
        content_type="application/json",
        text=json.dumps(
            {
                "sdp": pc.localDescription.sdp,
                "type": pc.localDescription.type}
        ),
    )


def set_weel(data, circular_queue):
    deltaY = float(data['deltaY'])
    user_envent_ms = float(data['timestampInMs'])
    ok = circular_queue.enqueue(
        np.array([1, deltaY, 0, 0, 0, 0, user_envent_ms, 0], dtype='float64'))
    ts = time.time()*1000
    logging.info(f'WEEL Time until enqueue {ts-user_envent_ms:.2f} ms')
    return ok


def set_mouse(data, circular_queue):
    x = float(data['x'])
    y = float(data['y'])
    ctrl_key = int(data['ctrlKey'])
    shift_key = int(data['shiftKey'])

    user_envent_ms = float(data['timestampInMs'])
    circular_queue = circular_queue
    ok = circular_queue.enqueue(
        np.array([2, 0, x, y,  ctrl_key, shift_key, user_envent_ms, 0], dtype='float64'))

    return ok


def set_mouse_click(data, circular_queue):
    # mouse left click 3
    # mouse left release 4
    # mouse right click 7
    # mouse right release 8
    # mouse middle click 5
    # mouse middle release 6
    on = 0 if data['on'] == 1 else 1
    ctrl = int(data['ctrlKey'])
    shift = int(data['shiftKey'])
    user_envent_ms = float(data['timestampInMs'])
    x = float(data['x'])
    y = float(data['y'])
    mouse_button = int(data['mouseButton'])
    if mouse_button not in [0, 1, 2]:
        return False
    if ctrl not in [0, 1] or shift not in [0, 1]:
        return False

    event_id = (mouse_button + 1)*2 + on + 1
    ok = circular_queue.enqueue(
        np.array([event_id, 0, x, y, ctrl, shift, user_envent_ms, 0], dtype='float64'))

    return ok


async def on_shutdown(app):
    # close peer connections
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)
    pcs.clear()
    for ws in set(app['websockets']):
        await ws.close(code=WSCloseCode.GOING_AWAY,
                       message='Server shutdown')


async def websocket_handler(request, **kwargs):

    circular_queue = kwargs['circular_queue']
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    request.app['websockets'].add(ws)
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                if msg.data == 'close':
                    await ws.close()
                else:
                    data = json.loads(msg.data)
                    logging.info(f'\n\nuser event time {data["timestampInMs"]}')
                    if data['type'] == 'weel':
                        ts = time.time()*1000
                        interval = ts-data['timestampInMs']
                        logging.info(
                            'WEEL request time approx ' +
                            f'{interval:.2f} ms')
                        set_weel(data, circular_queue)
                    elif data['type'] == 'mouseMove':
                        set_mouse(data, circular_queue)
                    elif data['type'] == 'mouseLeftClick':
                        set_mouse_click(data, circular_queue)
                    # await ws.send_str(msg.data + '/answer')

            elif msg.type == aiohttp.WSMsgType.ERROR:
                print('ws connection closed with exception %s' %
                    ws.exception())
    finally:
        request.app['websockets'].discard(ws)

    return ws


def get_app(
        rtc_server=None, folder=None, circular_queue=None,
        image_buffer_manager=None,
        broadcast=True):

    if folder is None:
        folder = f'{os.path.dirname(__file__)}/www/'

    app = web.Application()
    app['websockets'] = weakref.WeakSet()

    app.on_shutdown.append(on_shutdown)

    # app.router.add_get("/shutdown",
    #     partial(
    #         shutdown,
    #         app=app,
    #         rtc_server=rtc_server,
    #     )
    # )
    app.router.add_get("/", partial(index, folder=folder))
    js_files = ['main.js', 'webrtc.js', 'constants.js']
    for js in js_files:
        app.router.add_get(
            "/js/%s" % js, partial(javascript, folder=folder, js=js))

    if image_buffer_manager is not None:
        app['image_buffer_manager'] = image_buffer_manager
        app.router.add_get("/video/mjpeg", mjpeg_handler)

    
    if rtc_server is not None:
        app.router.add_post("/offer", partial(
            offer, video=rtc_server, broadcast=broadcast))

    if circular_queue is not None:
        app.add_routes([
            web.get(
                '/ws',
                partial(websocket_handler, circular_queue=circular_queue)
            )
        ])

    return app
