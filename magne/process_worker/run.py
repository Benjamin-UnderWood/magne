import os
import logging

import argparse

from magne.process_worker.master import main as magne_main


def get_parser():
    cpu_count = os.cpu_count()
    parser = argparse.ArgumentParser(prog='magne process', description='magne process worker')
    parser.add_argument('--task', type=str, help='task module path, default: magne.process_worker.demo_task', default='magne.process_worker.demo_task')
    parser.add_argument('--amqp-url', type=str, help='amqp address, default: amqp://guest:guest@localhost:5672//',
                        default='amqp://guest:guest@localhost:5672//',
                        )
    parser.add_argument('--workers', type=int, help='worker count, default: cpu count',
                        default=cpu_count,
                        )
    parser.add_argument('--worker-timeout', type=int, help='worker timeout, default 60s',
                        default=60,
                        )
    parser.add_argument('--qos', type=int, help='prefetch count, default qos=workers',
                        default=-1,
                        )
    parser.add_argument('--log-level', type=str, help='default: INFO',
                        default='INFO',
                        )
    return parser


def main():
    parser = get_parser()
    args = parser.parse_args()
    worker_nums = args.workers
    worker_timeout = args.worker_timeout
    task_module = args.task
    qos = args.qos if args.qos >= 0 else worker_nums
    amqp_url = args.amqp_url
    logger_level = args.log_level.upper()
    if logger_level not in logging._nameToLevel:
        raise Exception('invalid log level')
    logger_level = logging._nameToLevel[logger_level]
    magne_main(worker_nums, worker_timeout, task_module, amqp_url, qos, logger_level)
    return


if __name__ == '__main__':
    main()
