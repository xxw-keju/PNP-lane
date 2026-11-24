"""Installed entry point; setup never imports or modifies a proof worktree."""
import sys


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv and argv[0] in ('init', 'doctor', 'serve'):
            from .admin import main as admin_main
            return admin_main(argv)
        from .fastlane import main as pipeline_main
        return pipeline_main(argv)
    except (ValueError, OSError) as e:
        print('STOPPED: ' + str(e), file=sys.stderr)
        return 2
