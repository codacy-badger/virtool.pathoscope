import logging

import virtool.run.args
import virtool.run.logs

logger = logging.getLogger("aiohttp.server")

args = virtool.run.args.get_parser()

virtool.run.logs.configure()

if __name__ == "__main__":
    args.func(args)
