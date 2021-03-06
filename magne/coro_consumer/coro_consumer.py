'''
TODO: detect connection list lost
'''
import logging
import json
import importlib
import os
import signal
import struct

import pika
import curio
from curio import SignalQueue

from magne.logger import get_component_log
from magne.helper import BaseAsyncAmqpConnection, tasks as helper_tasks

CLIENT_INFO = {'platform': 'Python 3.6.3', 'product': 'coro amqp consumer', 'version': '0.1.0'}


class LarvaePool:

    def __init__(self, timeout, task_module, ack_queue, amqp_queue, log_level=logging.DEBUG, low_water=400, height_water=1000000):
        # TODO: detect connection lost, and wait for reconnect(event)
        self.ack = None
        self.timeout = timeout
        self.task_module = task_module
        self.watching = {}
        self.low_water, self.height_water = low_water, height_water
        self.logger = get_component_log('Magne-LarvaePool', log_level)
        self.water_event = curio.Event()
        self.alive = True
        self.ack_queue = ack_queue
        self.amqp_queue = amqp_queue
        self.wait_height_water = False
        return

    async def run(self):
        self.spawn_task = await curio.spawn(self.spawning)
        return

    async def spawning(self):
        # spawn consumers as many as we can
        while True:
            data = await self.amqp_queue.get()
            amqp_msg = [data]
            if self.amqp_queue.empty() is False:
                # fetch all data
                for d in self.amqp_queue._queue:
                    amqp_msg.append(d)
                self.amqp_queue._task_count = 0
                self.amqp_queue._queue.clear()
            for b in amqp_msg:
                self.logger.debug('got body: %s' % b)
                if len(self.watching) >= self.height_water:
                    # wait for low water
                    self.logger.info('waiting for low water, %s, %s' % (len(self.watching), self.height_water))
                    self.wait_height_water = True
                    await self.water_event.wait()
                    self.logger.info('now under low water')
                    self.wait_height_water = False
                    self.water_event.clear()
                ack_immediately = False
                try:
                    channel, devlivery_tag, data = b['channel'], b['delivery_tag'], json.loads(b['data'])
                    task_name, args = data['func'], data['args']
                    task = getattr(self.task_module, task_name)
                    assert task is not None
                except Exception:
                    ack_immediately = True
                    self.logger.error('invalid body frame: %s' % b, exc_info=True)
                broodling_task = await curio.spawn(self.broodling, channel, devlivery_tag, task, args, ack_immediately, daemon=True)
                self.logger.debug('spawn task %s(%s)' % (task_name, args))
                self.watching['%s_%s' % (channel, devlivery_tag)] = broodling_task
            self.logger.debug('spawning: %s done' % len(amqp_msg))
        return

    async def broodling(self, channel, devlivery_tag, task, args, ack_immediately=False):
        try:
            er = False
            msg = ''
            if ack_immediately is False:
                try:
                    # timeout will cancel coro
                    res = await curio.timeout_after(self.timeout, task, *args)
                except curio.TaskTimeout:
                    msg = 'task %s(%s) timeout' % (task, args)
                except Exception as e:
                    er = True
                    msg = 'timeout task %s(%s) exception: %s' % (task, args, e)
                else:
                    msg = 'task %s(%s) done, res: %s' % (task, args, res)
            if er is True:
                self.logger.error(msg, exc_info=True)
            else:
                self.logger.info(msg)
        except curio.CancelledError:
            self.logger.info('broodling %s canceled' % devlivery_tag)
        finally:
            await self.ack_queue.put((channel, devlivery_tag))
            del self.watching['%s_%s' % (channel, devlivery_tag)]
            if self.alive:
                if self.wait_height_water and len(self.watching) < self.low_water:
                    await self.water_event.set()
        return

    async def close(self, warm=True):
        # close all watch tasks
        # empty amqp queue
        await self.spawn_task.cancel()
        self.alive = False
        if warm is True:
            self.logger.info('waiting for watch tasks join, timeout: %s(s)' % self.timeout)
            try:
                async with curio.timeout_after(self.timeout):
                    async with curio.TaskGroup(self.watching.values()) as wtg:
                        await wtg.join()
            except curio.TaskTimeout:
                # all task would be canceled if task group join timeout!!!
                self.logger.info('watch task group join timeout...')
        else:
            self.logger.info('cold shutdown, cancel all watching tasks')
            for t in list(self.watching.values()):
                await t.cancel()
        return


class SpellsConnection(BaseAsyncAmqpConnection):
    logger_name = 'Magne-Connection'
    client_info = CLIENT_INFO

    def __init__(self, ack_queue, amqp_queue, *args, **kwargs):
        super(SpellsConnection, self).__init__(*args, **kwargs)
        self.ack_queue = ack_queue
        self.amqp_queue = amqp_queue
        self.fragment_frame = []
        self.ack_done = curio.Event()
        return

    async def run(self):
        await self.connect()
        await self.start_consume()
        self.fetch_task = await curio.spawn(self.fetch_from_amqp)
        self.wait_ack_task = await curio.spawn(self.wait_ack)
        return

    async def wait_ack(self):
        while True:
            data = await self.ack_queue.get()
            ack_msg = [data]
            if self.ack_queue.empty() is False:
                # fetch all data
                for d in self.ack_queue._queue:
                    ack_msg.append(d)
                self.ack_queue._task_count = 0
                self.ack_queue._queue.clear()
            self.ack_done.clear()
            for c_number, d_tag in ack_msg:
                await self.ack(c_number, d_tag)
            await self.ack_done.set()
            self.logger.debug('ack %s done, set ack_done: %s' % (len(ack_msg), self.ack_done.is_set()))
        return

    async def start_consume(self):
        # create amqp consumers
        for tag, queue_name in enumerate(self.queues):
            start_comsume = pika.spec.Basic.Consume(queue=queue_name, consumer_tag=str(tag))
            self.logger.debug('send basic.Consume %s %s' % (queue_name, str(tag)))
            frame_value = pika.frame.Method(self.channel_obj.channel_number, start_comsume)
            await self.sock.sendall(frame_value.marshal())
            data = await self.sock.recv(self.MAX_DATA_SIZE)
            count, frame_obj = pika.frame.decode_frame(data)
            if isinstance(frame_obj.method, pika.spec.Basic.ConsumeOk) is False:
                if isinstance(frame_obj.method, pika.spec.Basic.Deliver):
                    count = 0
                else:
                    raise Exception('got basic.ConsumeOk error, frame_obj %s' % frame_obj)
            self.logger.debug('get basic.ConsumeOk')
            # message data after ConsumeOk
            if len(data) > count:
                await self.parse_and_spawn(data[count:])
        self.logger.debug('start consume done!')
        return

    async def fetch_from_amqp(self):
        self.logger.info('staring fetch_from_amqp')
        try:
            while True:
                try:
                    data = await self.sock.recv(self.MAX_DATA_SIZE)
                except ConnectionResetError:
                    self.logger.error('fetch_from_amqp ConnectionResetError, wait for reconnect...')
                except curio.CancelledError:
                    self.logger.info('fetch_from_amqp cancel')
                    break
                except Exception as e:
                    self.logger.error('fetch_from_amqp error: %s' % e, exc_info=True)
                else:
                    await self.parse_and_spawn(data)
        except curio.CancelledError:
            self.logger.info('fetch_from_amqp canceled')
        return

    def fragment_frame_size(self, data_in):
        try:
            (frame_type, channel_number,
             frame_size) = struct.unpack('>BHL', data_in[0:7])
        except struct.error:
            return 0, None

        # Get the frame data
        frame_end = pika.spec.FRAME_HEADER_SIZE + frame_size + pika.spec.FRAME_END_SIZE
        return frame_end

    async def parse_and_spawn(self, data):
        # [Basic.Deliver, frame.Header, frame.Body, ...]
        count = 0
        last_body = {}
        if self.fragment_frame:
            last_body, frag_data = self.fragment_frame
            data = frag_data + data
            self.fragment_frame = []
        while data:
            # 不完整的frame只可能在第一个或者最后一个
            # 第一个的话, 意味着上一次的最后一个frame也是不完整的
            # 那么我们把这两个不完整的拼接起来
            try:
                count, frame_obj = pika.frame.decode_frame(data)
            except Exception as e:
                self.logger.error('decode_frame error: %s, %s' % (data, e), exc_info=True)
                self.fragment_frame.extend([last_body, data])
                break
            else:
                if frame_obj is None:
                    self.logger.error('fragment fragment frame: %s' % data)
                    self.fragment_frame.extend([last_body, data])
                    break
            data = data[count:]
            if getattr(frame_obj, 'method', None) and isinstance(frame_obj.method, pika.spec.Basic.Deliver):
                last_body = {'channel': frame_obj.channel_number,
                             'delivery_tag': frame_obj.method.delivery_tag,
                             'consumer_tag': frame_obj.method.consumer_tag,
                             'exchange': frame_obj.method.exchange,
                             'routing_key': frame_obj.method.routing_key,
                             }
            elif isinstance(frame_obj, pika.frame.Body):
                last_body['data'] = frame_obj.fragment.decode("utf-8")
                await self.amqp_queue.put(last_body)
                count += 1
                last_body = {}
        return

    async def preclose(self):
        await self.fetch_task.cancel()
        return

    async def close(self):
        self.logger.debug('ack queue empty: %s, ack_done.is_set: %s' % (self.ack_queue.empty(), self.ack_done.is_set()))
        if self.ack_queue.empty() is False and self.ack_done.is_set() is False:
            self.ack_done.clear()
            try:
                self.logger.info('wait 600(s) for ack done')
                await curio.timeout_after(600, self.ack_done.wait)
            except curio.TaskTimeout:
                self.logger.warning('wait ack timeout')
        await self.wait_ack_task.cancel()
        await self.send_close_connection()
        return


class Queen:
    name = 'Magne-Queue'

    def __init__(self, timeout, task_module, qos, amqp_url='amqp://guest:guest@localhost:5672//', log_level=logging.DEBUG):
        self.task_modue = importlib.import_module(task_module)
        self.timeout = timeout
        self.qos = qos
        self.log_level = log_level
        self.logger = get_component_log(self.name, log_level)
        self.amqp_url = amqp_url
        self.queues = list(helper_tasks.keys())
        return

    async def watch_signal(self):
        while True:
            # term for warm shutdown
            # int  for cold shutdown
            # hup  for reload
            ss = [signal.SIGTERM, signal.SIGINT, signal.SIGCHLD, signal.SIGHUP]
            sname = {i.value: i.name for i in ss}
            with SignalQueue(*ss) as sq:
                signo = await sq.get()
                self.logger.info('get signal: %s' % sname[signo])
                if signo == signal.SIGHUP:
                    self.logger.info('reloading...')
                    # TODO: reload, restart?
                    continue
                if signo == signal.SIGTERM:
                    self.logger.info('kill myself...warm shutdown')
                    await self.shutdown()
                else:
                    self.logger.info('kill myself...cold shutdown')
                    await self.shutdown(warm=False)
                break
        return

    async def start(self):
        self.logger.info('Queue pid: %s' % os.getpid())
        ack_queue = curio.Queue()
        amqp_queue = curio.Queue()

        self.con = SpellsConnection(ack_queue, amqp_queue, self.queues, self.amqp_url, self.qos, log_level=self.log_level)
        self.spawning_pool = LarvaePool(self.timeout, self.task_modue, ack_queue, amqp_queue, log_level=self.log_level)

        con_run_task = await curio.spawn(self.con.run)
        await con_run_task.join()

        pool_task = await curio.spawn(self.spawning_pool.run)
        await pool_task.join()

        signal_task = await curio.spawn(self.watch_signal)
        await signal_task.join()
        return

    async def shutdown(self, warm=True):
        await self.con.preclose()
        await self.spawning_pool.close(warm)
        await self.con.close()
        return


def main(timeout, task_module, qos, amqp_url='amqp://guest:guest@localhost:5672//', log_level=logging.DEBUG, curio_debug=False):
    queen = Queen(timeout, task_module, qos, amqp_url, log_level=log_level)
    curio.run(queen.start, with_monitor=curio_debug)
    return


if __name__ == '__main__':
    import sys
    log_level = logging.DEBUG
    if len(sys.argv) == 2 and '--log-level' in sys.argv[1]:
        log_level_name = sys.argv[1].split('=')[1]
        if log_level_name == 'INFO':
            log_level = logging.INFO
    main(30, 'magne.coro_consumer.demo_task', 0, log_level=log_level, curio_debug=True)
