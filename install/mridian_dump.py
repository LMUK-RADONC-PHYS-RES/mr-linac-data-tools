"""Entry point for the standalone executable.

    mridian-dump decode <Message.dump> [--out DIR] [--no-previews]
    mridian-dump video  <cine_dir> [--out DIR] [--speed S] [--fps N]
"""
import sys

import cine_to_video
import decode_message_dump

COMMANDS = {'decode': decode_message_dump.main, 'video': cine_to_video.main}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print('usage:\n' + __doc__.strip().split('\n', 1)[1].rstrip() +
              '\n\nRun "mridian-dump <command> --help" for the options.', file=sys.stderr)
        sys.exit(0 if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help') else 2)
    cmd = sys.argv[1]
    COMMANDS[cmd](sys.argv[2:], prog=f'mridian-dump {cmd}')


if __name__ == '__main__':
    main()
