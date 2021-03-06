'''
reference: dramatiq.benchmarks.bench
'''
import pylibmc
import pika
import os
import argparse
import subprocess
import time

from magne.process_worker.master import main as magne_process_main


counter_key = "magne-latench-bench-counter"
memcache_client = pylibmc.Client(["localhost"], binary=True)
memcache_pool = pylibmc.ClientPool(memcache_client, 8)


routing_key = exchange_name = queue_name = 'magne_latency_bench'.upper()


def en_queue(n):
    print('starting en_queue...')
    parameters = pika.URLParameters('amqp://guest:guest@localhost:5672/%2F')

    connection = pika.BlockingConnection(parameters)

    channel = connection.channel()
    print('decalaring exchange and queue')
    try:
        channel.exchange_declare(exchange_name)
        channel.queue_declare(queue_name)
        print('bind')
        channel.queue_bind(queue_name, exchange_name, routing_key)
    except Exception as e:
        print('decalare exchange and queue error: %s' % e)
        raise e
    print('config exchange and queue done')
    print('staring send tasks into rabbitmq')
    for _ in range(n):
        channel.basic_publish('MAGNE_LATENCY_BENCH',
                              'MAGNE_LATENCY_BENCH',
                              '{"func": "magne_latency_bench", "args": []}',
                              )
    print('en_queue done')
    return


def setup(count):
    print('benchmark magne...')
    en_queue(count)
    print('%s tasks in rabbitmq' % count)
    return


def run_magne(workers):
    magne_process_main(workers, 200, 'magne.benchmark.bench_tasks', amqp_url='amqp://guest:guest@localhost:5672//',
                       qos=workers, logger_level="INFO")
    return


def parse_argv():
    parser = argparse.ArgumentParser(prog='magne-bench', description='benchmark magne, reference: dramatiq.benchmarks.bench')
    parser.add_argument('--count', type=int, help='worker count, default: 100',
                        default=100,
                        )
    parser.add_argument('--workers', type=int, help='worker count, default: 8',
                        default=8,
                        )
    args = parser.parse_args()
    return args.count, args.workers


def main():
    print('pid: %s' % os.getpid())
    count, worker_numbers = parse_argv()
    print('task count: %s, worker: %s' % (count, worker_numbers))
    setup(count)
    with memcache_pool.reserve() as client:
        start_time = time.time()
        client.set(counter_key, 0)
        cm = ['env', 'PYTHONPATH=/opt/curio:/opt/magne:/opt/magne/magne', 'python3.6', '/opt/magne/magne/process_worker/run.py',
              '--task=magne.process_worker.bench_tasks', '--workers=%s' % worker_numbers, '--qos=%s' % 0,
              '--worker-timeout=120']
        print(' '.join(cm))
        proc = subprocess.Popen(cm)
        processed = 0
        while processed < count:
            processed = client.get(counter_key)
            print(f"{processed}/{count} messages processed\r", end="")
            time.sleep(0.1)

        duration = time.time() - start_time
        proc.terminate()
        proc.wait()
        print(f"Took {duration} seconds to process {count} messages.")
    return


if __name__ == '__main__':
    main()
