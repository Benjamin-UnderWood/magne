#####
magne
#####

Curio, RabbitMQ, Distributed Task Queue

Python >= 3.6, curio >= 0.8, pika >= 0.11.2


参考celery, dramatiq开发的分发任务应用, 并且加入了协程worker. 用curio重写跟网络有关的部分, 包括组件之间的数据交互.

根据worker的不同, 分为进程模式, 线程模式和协程模式.


使用
====

git clone或者download, 然后
---------------------------

.. code-block:: 

    pip install -r requirements.txt
    cd magne/magne

运行进程worker
--------------

.. code-block::

    python run.py process --help

运行coroutine消费者
-------------------

.. code-block::

    python run.py coroutine --help

   
TODO
=======

当restart/reload的时候

1. 进程模式的worker的管理参考gunicorn, 相对于强杀, 应该是让worker自己退出, 阻塞的话应该由用户去处理

2. 那么线程worker可以让worker自行退出, 对比进程worker总判断父进程的pid, 以及判断self.alive, 线程worker可以
   
   设置daemon=True, 主线程更新worker线程的alive变量

3. 但是协程worker的管理不能让worker自己退出, 因为协程序是进程的一个函数, 只能超时强杀, 有更好的方法吗


模型
====

整体抽象在 `这里 <https://github.com/allenling/magne/blob/master/how_it_works.rst>`_

进程worker
----------

创建进程去执行task

实现在 `这里 <https://github.com/allenling/magne/tree/master/magne/process_worker>`_

比起celery, 代码和整个结构上更简单, celery的代码我是真不想看了~~~

分离publisher和consumer的配置, publisher一端可以随便用哪种库来发msg, 这需要保证exchange和msg的格式对就好了~不像celery, consumer和publisher公用一套代码~~多麻烦

线程worker
----------

每个worker进程创建多个线程去执行task

实现在 `这里 <https://github.com/allenling/magne/tree/master/magne/thread_worker>`_

`threading.Thread的C实现 <https://github.com/allenling/LingsKeep/blob/master/python_source_code/python_thread.rst>`_

`python thread的同步对象实现(C) <https://github.com/allenling/LingsKeep/blob/master/python_source_code/python_thread_sync_primitive.rst>`_

coroutine消费者
---------------

实现在 `这里 <https://github.com/allenling/magne/tree/master/magne/coro_consumer>`_

关于python `asynchronous-io <https://github.com/allenling/LingsKeep/blob/master/python_source_code/python_asynchronous_api.rst>`_

spawn协程去执行task, 注意的是, task必须是curio定制的, 比如sleep必须是curio.sleep

测试
====


1. 测试环境: Ubuntu16.04 16G Intel(R) Core(TM) i5-4250U(4核)

2. 测试参考: `dramatiq <https://github.com/Bogdanp/dramatiq/blob/master/benchmarks/bench.py>`_

3. 测试延迟函数: latency_bench(随机sleep(n), n不大于10)

4. 绘制表格的 `库 <https://github.com/allenling/draw-docs-table>`_

进程模式
--------


该模式就是主线程获取rabbitmq的数据, 然后创建出n个子进程, 然后子进程只是执行任务而已, 子进程把结果发给主线程, 然后主线程去ack.

受限于进程数, 一般进程数不大于cpu个数, 所以限制了消费的速率, celery也是这个模式

+-------+----------------+----------+
|       +                +          +
| tasks + celery/process + dramatiq +
|       +                +          +
+-------+----------------+----------+
|       +                +          +
| 100   + 45.12s         + 6.52s    +
|       +                +          +
+-------+----------------+----------+

线程模式
--------

+-------+---------------+----------+
|       +               +          +
| tasks + thread worker + dramatiq +
|       +               +          +
+-------+---------------+----------+
|       +               +          +
| 100   + 9.93s         + 6.53s    +
|       +               +          +
+-------+---------------+----------+
|       +               +          +
| 1000  + 48.05s        + 39.56s   +
|       +               +          +
+-------+---------------+----------+

* 100个task的时候dramatiq始终没有测出sleep(10), 我甚至怀疑它作弊了~~~所以100个task的时候线程模式也是去掉sleep(10)来测试.

* 两者的ack速率都在20/s-30/s之间, dramatiq的峰值达30/s, 但是总体都在27, 而线程模型峰值达到27, 但是总体都在25左右.

* 关于queue, dramatiq是使用内置的queue, 而线程模式是使用curio的queue, 所以交互的时候多了一步curio的调用.

* 关于超时的话, dramatiq是用signal.setitimer来设定定时器, 而线程模式是使用协程来监视超时, 所以在处理任务的时候多了一步和curio的交互.


coroutine消费者
---------------

**应该配置高低水位, 因为如果无限制的允许spawn的话, 可能会吃满cpu.为了测试, 高水位设置尽可能高, 设置为100w个**

qos为0, 单进程的coroutine, dramatiq运行测试的时候默认是8个进程

+-------+-----------+----------+-----------------+
|       +           +          +                 +
| tasks + coroutine + dramatiq + dramatiq-gevent +
|       +           +          +                 +
+-------+-----------+----------+-----------------+
|       +           +          +                 +
| 100   + 5.33s     + 6.52     + 6.63            +
|       +           +          +                 +
+-------+-----------+----------+-----------------+
|       +           +          +                 +
| 1000  + 10.55s    + 39.57s   + 14.96s          +
|       +           +          +                 +
+-------+-----------+----------+-----------------+
|       +           +          +                 +
| 5000  + 11.15s    + 204.70s  + 15.37           +
|       +           +          +                 +
+-------+-----------+----------+-----------------+
|       +           +          +                 +
| 10000 + 11.96s    + 408.10s  + 23.47           +
|       +           +          +                 +
+-------+-----------+----------+-----------------+


* 按理来说, 100 tasks的时候, 也有可能应该出现有任务sleep(10)的情况, 但是dramatiq(gevent)却始终没有任务睡眠超过10秒的, 就很奇怪.

  **所以100 tasks的比较的时候, 大家的时间应该都等于task睡眠最长时间**, 因为此时任务切换消耗都很小, 总时间只和运行时间最长的任务有关.

* 可以看到, 1000+任务的时候, 协程总时间都是10秒左右, 并且增长是很小的, 此时时间消耗依然是和task最长睡眠时间有关.
  
  也就是说就算几千个任务, 协程调度的时候还是可以1s调度上千个, 说明 **任务切换** 在协程中是几乎没有消耗的
  
  **dramati(gevent)都有那么点消耗**, 所以task越多, 切换花销就越多, 总时间和task最长睡眠时间是无关的

* coroutine下:

  1. 5k个task, **一直spawn(3000+任务)的时候** 的过程中, cpu消耗峰值在50%左右
  
  2. 1w个task的时候, **一直spawn(7000+任务)的时候**, cpu峰值90%以上

* dramatiq-gevent下:

  1. 5k个task, 每一个worker的cpu峰值消耗都在15%左右
  
  2. 1w个task, 每一个worker的峰值在20%左右

小结
====

速度
----


这里速度是特定函数下的测试, 并不代表实际使用的情况

队列的消费的速率取决于消费者的数量, 协程最多, 想开多少个就开多少个, 线程其次, 进程最少.


协程更有效率
------------

因为协程创建开销很低, 也就是一个协程对象, 然后用户态自己调度协程, 调度的开销也很低, 但是相应的, cpu会高挺多的.

cpu高是因为用户代码频繁调度切换协程的关系,导致进程一直处于运行状态.

正因为协程特点是spawn起来非常便宜, 使用协程就是要发挥spawn的特点, 更合适io密集(**甚至可以说是只有io**)的场景, 比如你可以spawn很多协程去监视一些fd超时, 比如分发请求什么的等等~~

由于协程序是单进程的单线程的(一般), 那么任何阻塞代码(阻塞io或者计算密集任务)都会导致其他协程停止执行, 所以要小心.

现在python的异步io的"难点"在于工具不多
--------------------------------------

比如上面的coroutine消费者模式, 你的每一个task必须适应于curio, 比如sleep必须是curio.sleep等等, 否则consumer都不会yield, 这样就失去了协程的优势. 

又比如如果写一个协程http服务器, 那么如果业务的view不能yield的话, 协程服务器并没有什么意义

因为不yield的话就是卡在一个request上. 如果需要业务的view能够yield的话, 必须配套有比如reids, mysql这些工具.

但是现在并没有很多配套的工具, 现在社区还是处于构建协程调度库(curio, asyncio, trio等等)状态.

dramatiq线程模型
------------------

dramatiq和celery的区别就是一个是线程执行task, 一个是进程执行task, 并且dramatiq的worker进程会开amqp连接, 主进程不会建立连接, 所以连接数比celery多.

dramatiq比较快, 并且方便, 不需要有其他的定制(比如你的task必须适应curio), 是由os来调度~~加上gevent之后, 那是更快了.

线程模式是目前比较好的一个模式.

celery多进程的模式
--------------------

受限于worker进程没有开线程处理task, 一个worker进程主能处理一个task, 限制了消费者的数量~~~但是进程模式对于处理一些计算密集型任务比较好, 实现也比较简单.


