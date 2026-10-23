"""Compatibility launcher: use the shared FXViewer for H5 files."""
import sys
from viewer_fx import main
if __name__ == '__main__':
    sys.argv.insert(1, '--source')
    sys.argv.insert(2, 'h5')
    sys.exit(main())
