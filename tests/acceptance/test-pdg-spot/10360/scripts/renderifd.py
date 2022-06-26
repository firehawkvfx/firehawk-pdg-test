#
# Copyright (c) <2020> Side Effects Software Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# NAME:	        renderifd.py ( Python )
#
# COMMENTS:     mantra utility module.  This is passed as the
#               -P argument to mantra ifd renders.
#

from __future__ import print_function, absolute_import

import argparse
import os
import shlex
import subprocess
import sys

try:
    from pdgcmd import reportResultData, localizePath, printlog
except ImportError:
    from pdgjob.pdgcmd import reportResultData, localizePath, printlog

def filterOutputAssets(assets):
    """
    filter callback function
    """
    # asset file-type -> pdg data tag
    resulttag = { 0: 'image',
                  1: 'image/texture',
                  2: 'geo',
                  3: 'image/shadowmap',
                  4: 'image/photonmap',
                  5: 'image/envmap' }

    for a in assets:
        # Foreach file in asset
        for f in a[1]:
            filename = f[0]
            tag = ""
            try:
                tag = 'file/' + resulttag[f[1]]
            except:
                pass
            reportResultData(filename, result_data_tag=tag)

if __name__ == "__main__":
    # mantra will exec this file, so we need to detect
    # that case and exit early
    if len(sys.argv) == 1:
        sys.exit(0)
    parser = argparse.ArgumentParser(description="Invokes mantra on a given ifd file")

    parser.add_argument('ifd', help="The .ifd file to read")
    parser.add_argument('--picture', required=False, help="Override the output file to write")
    parser.add_argument('--hfs', required=False, help="HFS for mantra")
    parser.add_argument('extra', nargs=argparse.REMAINDER, help='extra arguments to pass '
                        'to the mantra command line')

    args = parser.parse_args()

    extra_argv = []
    if args.extra:
        for extra in args.extra:
            extra_argv.extend(shlex.split(extra))

    filter_path =  __file__.replace('\\', '/')
    ifd_path = localizePath(args.ifd)

    if args.hfs:
        bin_path = os.path.expandvars(args.hfs + "/bin/mantra")
    else:
        if 'HFS' in os.environ:
            bin_path = os.path.expandvars('$HFS/bin/mantra')
        else:
            bin_path = 'mantra'

    mantra_argv = [bin_path, '-P', filter_path, '-f', ifd_path]
    mantra_argv.extend(extra_argv)
    if args.picture:
        mantra_argv.append(args.picture)

    printlog("Calling: " + " ".join(mantra_argv))
    res = subprocess.call(mantra_argv, shell=False)
    sys.exit(res)
