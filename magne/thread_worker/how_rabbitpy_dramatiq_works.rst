rabbitpy和dramatiq线程模式
================================

两个库如何io, 如何执行task的


rabbitpy的例子
------------------

rabbitpy主线程会开启一个io线程, io线程去send, recv, recv的时候看是哪个channel, 然后把msg发给对应的子线程~~~所以线程数量=主线程+io线程+N个子线程

实例代码: https://rabbitpy.readthedocs.io/en/latest/threads.html


.. code-block:: python

    def consumer(connection):
        received = 0
        with connection.channel() as channel:
            for message in rabbitpy.Queue(channel, QUEUE).consume_messages():
                print(message.body)
                message.ack()
                received += 1
                print('received: %s' % received)
                time.sleep(10)
                if received == MESSAGE_COUNT:
                    break

    def main():
        这里当实例化Connection的时候, 已经开启一个io线程去建立连接了
        with rabbitpy.Connection() as connection:
	    kwargs = {'connection': connection}
        # 开启子线程
        consumer_thread = threading.Thread(target=consumer, kwargs=kwargs)
	consumer_thread.start()

实例化Connection
~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

    class Connection(base.StatefulObject):
        def __init__(self, url=None):
            self._connect()
        def _connect(self):
            self._set_state(self.OPENING)
    	    # 开启一个io线程
            self._io = self._create_io_thread()
            self._io.daemon = True
            self._io.start()

所以当在子线程里面调用with connection的时候, 已经是建立好了的connection

开启channel
~~~~~~~~~~~~


.. code-block:: python

    class Connection(base.StatefulObject):

        def channel(self, blocking_read=False):

            with self._channel_lock:
	        # 获取channel的id
                channel_id = self._get_next_channel_id()
		# 这个channel_frames就是channel的_read_queue
		# channel实例化的时候第五个参数就是了
                channel_frames = queue.Queue()
		# 这里创建channel
                self._channels[channel_id] = channel.Channel(channel_id, self.capabilities,
                                                             self._events,
                                                             self._exceptions,
                                                             channel_frames,
                                                             self._write_queue,
                                                             self._max_frame_size,
                                                             self._io.write_trigger,
                                                             self,
                                                             blocking_read)
	        # 这里的_add_channel_to_io就是IO._channels[int(channel)] = channel, write_queue
		# 这里的write_queue是io的写queue, 对应来说就是channel的_read_queue, 也就是channel_frame
		# channel被保存到io线程内的字典而已

                self._add_channel_to_io(self._channels[channel_id], channel_frames)

		# 这里的open就是构建channel.open的frame, 然后通过write_trigger
		# 来让io线程去发送frame, write_trigger就是一个socket, 对这个socket发送一个字符, io线程收到提醒就发送
		# 缓存区(self._write_queue)里面的数据了

                self._channels[channel_id].open()
                return self._channels[channel_id]

所以, 子线程中开启channel也是通过io线程来完成

send/rev
~~~~~~~~~

接下来是接收frame和分配到子线程的过程

整个send/rev都是在io线程完成的


.. code-block:: python

    # rabbitpy.io.IO

    class IO(threading.Thread, base.StatefulObject):

        def run(self):
            self._connect()# io线程启动的时候先去连接, 然后开启io loop
            self._loop = _IOLoop(
                self._socket, self.on_error, self.on_read, self.on_write,
                self._write_queue, self._events, self._write_listener,
                self._exceptions)
            # 启动loop, 这个loop就是epoll的poll了, 这里注册了self.on_read作为读取到数据时候的回调
            self._loop.run() 

        def on_read(self, data):
            # on_read就是读取到数据的时候的回调
            self._buffer += data 

            while self._buffer:
                # value是已经解包好的数据, value[0]是channel的id, value[1]是数据, 这里把数据发送给对应的channel线程
                self._add_frame_to_read_queue(value[0], value[1]) 

        def _add_frame_to_read_queue(self, channel_id, frame_value):
            self._channels[channel_id][1].put(frame_value) # channel初始化的是会把自己和自己的write_queu注册

然后呢, channel如何拿到frame? 

.. code-block:: python

    # 这一句呢, 最后会回到rabbitpy.base.AMQPChannel._wait_on_frame中了
    for message in rabbitpy.Queue(channel, QUEUE).consume_messages():
        pass

channel等待frame的到来

.. code-block:: python

    # rabbitpy.base.AMQPChannel._wait_on_frame
    def _wait_on_frame(self, frame_type=None):
        start_state = self.state
        self._waiting = True
        while (start_state == self.state and
                not self.closed and
                not self._connection.closed):
            value = self._read_from_queue() # 这一句就是等待之前io线程的write_queue有数据了

ack的过程
~~~~~~~~~~~~

ack呢也是把数据发送给io线程, 让它去发送的了

.. code-block:: python

    # rabbitpy.message.Message.ack
    def ack(self, all_previous=False):
        # 这里就是把ack通过channel来发送, 流程和开启channel的时候一样, write_trigger
        self.channel.write_frame(basic_ack) 


dramatiq例子
---------------

dramatiq也是一样, 主线程孵化出io线程和逻辑线程, 然后io线程和逻辑线程通过queue交互

dramatiq中每一个queue会创建一个io线程, 默认有8个逻辑线程

dramatiq和rabbitpy差不多, 都是io线程分配msg给逻辑线程, 区别是:

1. rabbitpy是一个io线程, 每一个逻辑线程创建一个channel, 然后io线程分配msg到对应的逻辑线程.

   rabbitpy的逻辑线程不是thread pool, 因为每一个thread只能执行对应的channel的task

2. dramatiq是有多少个queue就有多少个channel, 每个channel对应一个queue对应一个io线程, io线程分配给逻辑线程.

   dramatiq的逻辑线程更像是一个thread pool, N个io线程去把msg发送给M个逻辑线程

consumer线程
~~~~~~~~~~~~~~~~

consumer线程也就是io线程


.. code-block:: python

    # dramatiq.worker.Worker._add_consumer
    class Worker:
        # 这里_add_consumer传入的参数是queue_name, 说明一个queue一个消费线程
        def _add_consumer(self, queue_name, *, delay=False):
            if queue_name in self.consumers:
                return
            consumer = self.consumers[queue_name] = _ConsumerThread(
                broker=self.broker,
                queue_name=queue_name,
                prefetch=self.delay_prefetch if delay else self.queue_prefetch,
                work_queue=self.work_queue,
                worker_timeout=self.worker_timeout,
            )
            consumer.start()

ConsumerThread类

.. code-block:: python

    # dramatiq.worker._ConsumerThread
    class _ConsumerThread(Thread):
        def run(self, attempts=0):
            try:
                self.logger.debug("Running consumer thread...")
                self.running = True
                # 这里self.consumer是broker的consume迭代器
                # 基本上作用就是返回msg了, 细节不用管
                self.consumer = self.broker.consume(
                    queue_name=self.queue_name,
                    prefetch=self.prefetch,
                    timeout=self.worker_timeout,
                )
                attempts = 0
    	        # 循环处理msg
                for message in self.consumer:
                    if message is not None:
                        # 处理msg
                        self.handle_message(message)
    
                    self.handle_acks()
                    self.handle_delayed_messages()
                    if not self.running:
                        break
    
            except ConnectionError:
                pass

        def handle_message(self, message):
            try:
                if "eta" in message.options:
                    self.logger.debug("Pushing message %r onto delay queue.", message.message_id)
                    self.broker.emit_before("delay_message", message)
                    self.delay_queue.put((message.options.get("eta", 0), message))

                else:
                    # actor就是task的名称了
                    actor = self.broker.get_actor(message.actor_name)
                    self.logger.debug("Pushing message %r onto work queue.", message.message_id)
                    # 这里把msg加入到worker_queue中, worker_queue就是和其他逻辑线程交互的地方
                    self.work_queue.put((actor.priority, message))
            except ActorNotFound:
                pass

        def handle_acks(self):
            # 这里处理ack, 显然通过acks_queue这个队列来处理
            for message in iter_queue(self.acks_queue):
                if message.failed:
                    self.logger.debug("Rejecting message %r.", message.message_id)
                    self.broker.emit_before("nack", message)
                    self.consumer.nack(message)
                    self.broker.emit_after("nack", message)
                else:
                    self.logger.debug("Acknowledging message %r.", message.message_id)
                    self.broker.emit_before("ack", message)
                    self.consumer.ack(message)
                    self.broker.emit_after("ack", message)
                self.acks_queue.task_done()

**所以consumer线程都是通过queue和其他逻辑线程交互的了**


worker线程
~~~~~~~~~~~~~

worker线程是一个thread pool的形式, 接收msg, 然后执行, 不像rabbitpy中, 每一个线程只能执行唯一一个channel的msg

**执行msg**

.. code-block:: python

    # dramatiq.worker._WorkerThread
    class _WorkerThread(Thread):
    
        def run(self):
            self.running = True
            while self.running:
                if self.paused:
                    self.logger.debug("Worker is paused. Sleeping for %.02f...", self.timeout)
                    self.paused_event.set()
                    time.sleep(self.timeout)
                    continue
    
                try:
                    # 从worker_queue中拿到需要处理的msg
                    _, message = self.work_queue.get(timeout=self.timeout)
                    # ack的过程
                    self.process_message(message)
                except Empty:
                    continue

        def process_message(self, message):
            try:
                res = None
                if not message.failed:
                    # 拿到actor, 也就是task对应的函数
                    actor = self.broker.get_actor(message.actor_name)
                    # 执行task
                    res = actor(*message.args, **message.kwargs)
            except SkipMessage as e:
                self.logger.warning("Message %s was skipped.", message)
                self.broker.emit_after("skip_message", message)

            except BaseException as e:
                self.logger.warning("Failed to process message %s with unhandled exception.", message, exc_info=True)
                self.broker.emit_after("process_message", message, exception=e)

            finally:
                # 这里的post_process_message将会把msg添加到ack的queue中
                # 这里post_process_message是_ConsumerThread中的方法
                self.consumers[message.queue_name].post_process_message(message)
                self.work_queue.task_done()

**ack过程**


.. code-block:: python

    class _ConsumerThread(Thread):
        def post_process_message(self, message):
            # 把msg放入到acks_queue中
            self.acks_queue.put(message)
            # 发送中断是为了唤醒consumer线程
            self.consumer.interrupt()





小结
----------

所以所谓的一个线程一个channel就是每一个线程负责消费对应channel的数据, 然后所有的send/recv都由io线程来执行, recv的时候通过queue来唤醒对应的线程.

**那么, 为什么一个channel还不够呢?多个channel的话感觉就很麻烦呀~~~**

既然都是靠一个单独的io线程来分配msg, 那么多个channel的意义呢? 感觉只有每一个channel都能单独send/recv才有单独出来的意义呀

不然多个线程的收发的瓶颈还是在io线程上, 分离channel并不能提高收发, 不如一个channel一个connection, 然后

产生thread pool, 把msg扔到thread pool去执行~~~~这样更简单

所以最后的做法是跟coro_consumer一样, 只不过coroutine换成了curio中的async thread
