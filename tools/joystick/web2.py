import asyncio
import json
import logging
import os
import ssl
import uuid
import time

from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription

import cereal.messaging as messaging
from tools.joystick.bodyav import BodyMic, BodyVideo, WebClientSpeaker, force_codec, play_sound, MediaBlackhole
from tools.joystick.bodyav import yolow

os.environ['OPENBLAS_NUM_THREADS'] = '1'
os.environ['MKL_NUM_THREADS'] = '1'

logger = logging.getLogger("pc")
logging.basicConfig(level=logging.INFO)

pcs = set()

pm = messaging.PubMaster(['testJoystick'])
sm = messaging.SubMaster(['carState', 'logMessage'])
FIND_OBJ = 'person'


async def index(request):
  content = open("./static/index.html", "r").read()
  now = time.monotonic()
  request.app['mutable_vals']['last_send_time'] = now
  request.app['mutable_vals']['last_override_time'] = now
  request.app['mutable_vals']['prev_command'] = []
  request.app['mutable_vals']['show_yolo'] = False
  request.app['mutable_vals']['find_person'] = False

  return web.Response(content_type="text/html", text=content)


async def control_body(data, app):
  now = time.monotonic()
  if (data['type'] == 'dummy_controls') and (now < (app['mutable_vals']['last_send_time'] + 0.2)):
    return
  if data['type'].startswith('yolo') and (now < (app['mutable_vals']['last_override_time'] + 0.2)):
    return
  if (data['type'] == 'control_command') and (app['mutable_vals']['prev_command'] == [data['x'], data['y']] and data['x'] == 0 and data['y'] == 0):
    return
  
  
  print(data)
  x = max(-1.0, min(1.0, data['x']))
  y = max(-1.0, min(1.0, data['y']))
  dat = messaging.new_message('testJoystick')
  dat.testJoystick.axes = [x, y]
  dat.testJoystick.buttons = [False]
  pm.send('testJoystick', dat)
  app['mutable_vals']['last_send_time'] = now
  if (data['type'] == 'control_command'):
    app['mutable_vals']['last_override_time'] = now
    app['mutable_vals']['prev_command'] = [data['x'], data['y']]
    


async def dummy_controls_msg(app):
  while True:
    if 'last_send_time' in app['mutable_vals']:
      this_time = time.monotonic()
      if (app['mutable_vals']['last_send_time'] + 0.2) < this_time:
        await control_body({'type': 'dummy_controls', 'x': 0, 'y': 0}, app)
    await asyncio.sleep(0.2)


async def yolo(app):
  while True:
    if ('last_send_time' in app['mutable_vals']) and app['mutable_vals']['show_yolo']:
      # app['mutable_vals']['yolo'] = []
      sm.update(timeout=0)
      if sm['logMessage']:
        msg = json.loads(sm['logMessage'])
        app['mutable_vals']['yolo'] = msg
    await asyncio.sleep(0.1)


def get_yolo_plan(app, width, tol=0.1):
  if ('last_send_time' in app['mutable_vals']) and ('yolo' in app['mutable_vals']) and ('find_person' in app['mutable_vals']) and app['mutable_vals']['find_person']:
    find_obj = FIND_OBJ
    yolo_objects = app['mutable_vals']['yolo']
    for yolo_object in yolo_objects:
      if yolo_object['pred_class'] == find_obj:
        pos = ((yolo_object['pt1'][0] + yolo_object['pt2'][0]) / 2 / width)
        if 0.5 - tol < pos < 0.5 + tol:
          return {'type': 'yolo_person_found', 'x': -1, 'y': 0}
        elif 0.5 - tol > pos:
          return {'type': 'yolo_escaping_left', 'x': 0, 'y': 1}
        else:
          return {'type': 'yolo_escaping_right', 'x': 0, 'y': -1}
    return {'type': 'yolo_searching', 'x': 0, 'y': -1}
  return None


async def go_to_object(app):
  prev_plan = [0, 0]
  while True:
    plan = get_yolo_plan(app, yolow)
    if plan:
      await control_body(plan, app)
    await asyncio.sleep(0.05)


async def start_background_tasks(app):
  app['bgtask_dummy_controls_msg'] = asyncio.create_task(dummy_controls_msg(app))
  app['bgtask_dummy_yolo'] = asyncio.create_task(yolo(app))
  app['bgtask_dummy_go_to_object'] = asyncio.create_task(go_to_object(app))


async def stop_background_tasks(app):
  app['bgtask_dummy_controls_msg'].cancel()
  app['bgtask_dummy_yolo'].cancel()
  app['bgtask_dummy_go_to_object'].cancel()
  await app['bgtask_dummy_controls_msg']
  await app['bgtask_dummy_yolo']
  await app['bgtask_dummy_go_to_object']


async def offer(request):
  logger.info("\n\n\nnewoffer!\n\n")

  params = await request.json()
  offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
  speaker = WebClientSpeaker()
  blackhole = MediaBlackhole()

  pc = RTCPeerConnection()
  pc_id = "PeerConnection(%s)" % uuid.uuid4()
  pcs.add(pc)

  def log_info(msg, *args):
    logger.info(pc_id + " " + msg, *args)

  log_info("Created for %s", request.remote)

  @pc.on("datachannel")
  def on_datachannel(channel):
    request.app['mutable_vals']['remote_channel'] = channel

    @channel.on("message")
    async def on_message(message):
      data = json.loads(message)
      if data['type'] == 'control_command':
        await control_body(data, request.app)
        times = {
          'type': 'ping_time',
          'incoming_time': data['dt'],
          'outgoing_time': int(time.time() * 1000),
        }
        channel.send(json.dumps(times))
      if data['type'] == 'battery_level':
        sm.update(timeout=0)
        if sm.updated['carState']:
          channel.send(json.dumps({'type': 'battery_level', 'value': int(sm['carState'].fuelGauge * 100)}))
      if data['type'] == 'play_sound':
        await play_sound(data['sound'])
      if data['type'] == 'show_yolo':
        request.app['mutable_vals']['show_yolo'] = data['value']
        request.app['mutable_vals']['yolo'] = []
      if data['type'] == 'find_person':
        request.app['mutable_vals']['find_person'] = data['value']


  @pc.on("connectionstatechange")
  async def on_connectionstatechange():
    log_info("Connection state is %s", pc.connectionState)
    if pc.connectionState == "failed":
      await pc.close()
      pcs.discard(pc)

  @pc.on('track')
  def on_track(track):
    print(f"Track received: {track.kind}")
    if track.kind == "audio":
      speaker.addTrack(track)
    elif track.kind == "video":
      blackhole.addTrack(track)

    @track.on("ended")
    async def on_ended():
      log_info("Remote %s track ended", track.kind)
      if track.kind == "audio":
        await speaker.stop()
      elif track.kind == "video":
        await blackhole.stop()

  video_sender = pc.addTrack(BodyVideo(request.app))
  force_codec(pc, video_sender)
  _ = pc.addTrack(BodyMic())

  await pc.setRemoteDescription(offer)
  await speaker.start()
  await blackhole.start()
  answer = await pc.createAnswer()
  await pc.setLocalDescription(answer)

  return web.Response(
    content_type="application/json",
    text=json.dumps(
      {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
    ),
  )


async def on_shutdown(app):
  coros = [pc.close() for pc in pcs]
  await asyncio.gather(*coros)
  pcs.clear()


async def run(cmd):
  proc = await asyncio.create_subprocess_shell(
    cmd,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE
  )
  stdout, stderr = await proc.communicate()
  print("Created key and cert!")
  if stdout:
    print(f'[stdout]\n{stdout.decode()}')
  if stderr:
    print(f'[stderr]\n{stderr.decode()}')


if __name__ == "__main__":
  # App needs to be HTTPS for microphone and audio autoplay to work on the browser
  if (not os.path.exists("cert.pem")) or (not os.path.exists("key.pem")):
    asyncio.run(run('openssl req -x509 -newkey rsa:4096 -nodes -out cert.pem -keyout key.pem -days 365 -subj "/C=US/ST=California/O=commaai/OU=comma body"'))
  else:
    print("Certificate exists!")
  ssl_context = ssl.SSLContext()
  ssl_context.load_cert_chain("cert.pem", "key.pem")
  app = web.Application()
  app['mutable_vals'] = {}
  app.on_shutdown.append(on_shutdown)
  app.router.add_post("/offer", offer)
  app.router.add_get("/", index)
  app.router.add_static('/static', 'static')
  app.on_startup.append(start_background_tasks)
  app.on_cleanup.append(stop_background_tasks)
  web.run_app(app, access_log=None, host="0.0.0.0", port=5000, ssl_context=ssl_context)