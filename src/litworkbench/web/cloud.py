"""One-process cloud entry point. No implicit workspace creation or proxy trust."""
import argparse

from litworkbench.runtime import RuntimeSettings
from litworkbench.web.app import create_app


def main(argv=None):
    parser = argparse.ArgumentParser(description='Start the authenticated cloud workspace.')
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', default=8000, type=int)
    args = parser.parse_args(argv)
    settings = RuntimeSettings.from_env()
    if settings.mode != 'cloud':
        raise ValueError('The cloud entry point requires LIT_MODE=cloud')
    import uvicorn
    uvicorn.run(create_app(args.workspace, settings=settings), host=args.host, port=args.port,
                workers=1, proxy_headers=False, access_log=False)


if __name__ == '__main__':
    main()
