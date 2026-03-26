# Copyright 2022 99cloud
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import multiprocessing
import os
os.environ['OPENBLAS_NUM_THREADS'] = '1'

bind = "0.0.0.0:38887"
workers = 4
worker_class = "uvicorn.workers.UvicornWorker"
timeout = 300
keepalive = 5
reuse_port = True
proc_name = "dingo-command"

# Use gunicorn native log file settings instead of logconfig_dict file handlers,
# which are unreliable with RotatingFileHandler in gunicorn's worker model.
accesslog = "/var/log/dingo-bear/dingo-bear-access.log"
errorlog = "/var/log/dingo-bear/dingo-bear-error.log"
loglevel = "info"

logconfig_dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "root": {"level": "INFO", "handlers": ["console"]},
    "loggers": {
        "gunicorn.error": {"level": "INFO", "handlers": ["console"], "propagate": 0},
        "gunicorn.access": {"level": "INFO", "handlers": ["console"], "propagate": 0},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "level": "INFO",
            "formatter": "generic",
        },
    },
    "formatters": {
        "generic": {
            "format": "%(asctime)s.%(msecs)03d %(process)d %(levelname)s [-] %(message)s",
            "datefmt": "[%Y-%m-%d %H:%M:%S %z]",
            "class": "logging.Formatter",
        }
    },
}
