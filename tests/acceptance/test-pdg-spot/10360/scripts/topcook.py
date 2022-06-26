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
# NAME:	        topcook.py ( Python )
#
# COMMENTS:     Utility program for executing top networks
#

from __future__ import print_function, absolute_import

import argparse
import operator
import os
import traceback
import sys
import socket
import threading
import time
import platform
try:
    import urllib.parse as urlparse
except ImportError:
    import urlparse

from collections import Counter

import hqueue.houdini as hq
import hou
import pdg

try:
    import pdgcmd
    import pdgjson
except ImportError:
    from pdgjob import pdgcmd
    from pdgjob import pdgjson

import pdgd
import pdgd.util

printlog = pdgcmd.printlog

def startDataLayerServer(port, iface='0.0.0.0'):
    """
    Starts a Data Layer Server at the given port, 0 means choose a free one.
    This will be a websocket-based server.
    FIXME: bind to given interface
    """
    server = pdgd.DataLayerServerManager.Instance().createServer(
        'DataLayerWSServer', port)
    server.serve()
    bound_port = server.getPort()

    try:
        net_hostname = socket.getfqdn()
        ip_addr = socket.gethostbyname(net_hostname)
    except:
        try:
            net_hostname = platform.node()
            ip_addr = socket.gethostbyname(net_hostname)
        except:
            net_hostname = socket.gethostname()
            ip_addr = socket.gethostbyname(net_hostname)
    
    return (ip_addr, bound_port)

def cookTopNode(node_path, taskgraph='', print_logs=False, verbosity=1):
    """
    Does a PDG cook of the given TOP node.
    The TOP node is first cooked in Houdini to create the
    PDG node and all dependent upstream nodes.  Then the
    PDG graph is executed with respect to the with the given node's
    PDG node.
    Any PDG errors are collected and printed to stdout.
    The cook is considered 'failed' if the display node has an error, or has a
    work item with an error state
    Returns (cook_failed, displaynode TOP)
    """
    node = hou.node(node_path)
    if not node:
        raise RuntimeError("Cannot find node %s" % node_path)

    disp_node = node.displayNode()
    if not disp_node:
        # this is probably a node within the topnet,
        # force the displayflag if necessary
        if node != node.parent().displayNode():
            node.setDisplayFlag(True)
        disp_node = node.parent().displayNode()

    printlog("Given Node '{}', Cooking Node '{}'".format(node, disp_node))

    if taskgraph:
        # cook the TOP net so that PDG nodes exist
        disp_node.cookWorkItems(block=True, tops_only=True)
        disp_node.getPDGGraphContext().deserializeWorkItems(taskgraph)
        nodes = disp_node.getPDGGraphContext().graph.nodes()
        #for n in nodes:
        #    for wi in n.workItems:
        #        print('{}, state={}'.format(wi.name, wi.state))
        count = sum([len(n.workItems) for n in nodes])
        printlog("Loaded %d items into graph" % count)

    # blocking cook of PDG
    disp_node.cookWorkItems(block=True)
    print("Finished Cook")

    def checked_print_log(wi):
        if print_logs:
            if wi.state == pdg.workItemState.CookedFail:
                log_uri = wi.node.scheduler.getLogURI(wi)
                parsed = urlparse.urlparse(log_uri)
                if parsed.scheme == "file":
                    logpath = wi.node.scheduler.localizePath(parsed.path)
                    if logpath.startswith("/"):
                        logpath = logpath[1:]
                try:
                    with open(logpath, 'r') as log:
                        print(log.read())
                except IOError:
                    pass
    batches = set()
    state_str = { getattr(pdg.workItemState, st) : 
        st for st in dir(pdg.workItemState) if not st.startswith('__')}
    state_str[pdg.workItemState.CookedCache] =\
        'workItemState.CookedCache ('\
        'Expected output files found, did not recook)'
    print("Work Item States:")
    ctx = disp_node.getPDGGraphContext()
    for node in ctx.graph.nodes():
        for wi in node.workItems:
            if wi.batchParent:
                batches.add(wi.batchParent)
            else:
                print('{:<40} {:>30}'.format(wi.name, state_str[wi.state]))
            checked_print_log(wi)

    for batch in batches:
        states = Counter([subitem.state for subitem in batch.batchItems])
        print("{} Subitems State Counts:".format(batch.name))
        for state, count in states.items():
            print("{:<40} {:>5}".format(state_str[state], count))

    print("")

    # print all TOP errors/warnings
    had_error = False
    for child in disp_node.parent().children():
        errors = child.errors()
        if errors and verbosity > 0:
            print("")
            had_error = True
            print('Errors from node {}:'.format(child))
            for error in errors:
                print(error)

        warnings = child.warnings()
        if warnings and verbosity > 1:
            print("")
            had_error = True
            print('Warnings from node {}:'.format(child))
            for warning in warnings:
                print(warning)

    if had_error:
        sys.stdout.flush()
    
    # determine failed state of the cook
    node = disp_node.getPDGNode()
    cook_failed = False
    if node:
        if node.hasErrors:
            cook_failed = True
        else:
            def isError(wi):
                if wi.batchParent:
                    wi = wi.batchParent
                return wi.state == pdg.workItemState.CookedFail
            try:
                next(wi for wi in node.workItems if isError(wi))
                cook_failed = True
            except StopIteration:
                pass
    return (cook_failed, disp_node)

def report_resultdata(pdg_node, report_all):
    """
    Collect the result data for the display node workitems
    report_all      True means collect and report all result data (DEPRECATED)
    """
    if report_all:
        nodes = pdg_node.context.graph.nodes()
        print("WARNING --report 'all' option is deprected and will be removed")
    else:
        nodes = [pdg_node]
    nodes = [n for n in nodes if n]

    for node in sorted(nodes, key=operator.attrgetter('name')):
        for wi in node.workItems:
            for result in wi.resultData:
                # localize using the originating scheduler, it will
                # be delocalized using our scheduler env var
                data_loc = node.scheduler.localizePath(result.path)
                pdgcmd.reportResultData(
                    data_loc,
                    result_data_tag=result.tag,
                    hash_code=result.hash)

def serialize_workitems(pdg_node, item_prefix, data_dir):
    """
    Serializes the workitems on the given node to the given directory.
    """
    workitems = pdg_node.workItems
    context = pdg_node.context
    pdg_dir = os.path.normpath(os.path.expandvars(os.environ['PDG_DIR'])).replace('\\', '/')
    if workitems:
        data_files = []
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)
        parents = set()
        for work_item in workitems:
            bp = work_item.batchParent
            if bp:
                if bp not in parents:
                    parents.add(bp)
                    work_item = bp
                else:
                    continue
            item_json_path = '{}/{}{}.json.gz'.format(
                    data_dir, item_prefix, work_item.name)
            # remap delocalized paths - if PDG_DIR is different for the fetched
            # graph (us) than it is for the fetching graph, we need to translate
            # our results to be relative to the fetching graph's
            local_wd = pdg_node.scheduler.workingDir(True)
            remote_wd = pdg_dir
            if local_wd != remote_wd:
                rebase_suffix = os.path.relpath(local_wd, remote_wd)
                rebased_pdg_dir = '__PDG_DIR__/' + rebase_suffix
                new_results = []
                for result in work_item.resultData:
                    if result.tag.startswith('file'):
                        data = result.path.replace('__PDG_DIR__', rebased_pdg_dir)
                    new_results.append((data, result.tag, result.hash))
                work_item.clearResultData()
                for data, tag, hc in new_results:
                    work_item.addResultData(data, tag, hc, True)

            work_item.saveJSONFile(item_json_path)
            data_files.append(item_json_path)

        # report all the generated files
        pdgcmd.reportResultData(data_files, result_data_tag='file/json/workitem')

def _startDataLayerServerWithTerminate(port, terminateEvent):
    ip_addr, bound_port = startDataLayerServer(port)
    # This line will be scanned from our stdout to find out the PDGD endpoint
    info = 'Data Layer Server: {}:{}'.format(ip_addr, bound_port)
    if args.datalayerfile:
        with open(args.datalayerfile, 'w') as fout:
            fout.write(info)
    printlog(info, timestamp=False)

    # Set up a subscription handler to recieve the TerminateRemoteGraph 
    # command from the submitter PDGD client
    class TerminateRemoteGraphHandler(pdgd.util.SimpleHandler):
        def __init__(self, handler): 
            super(TerminateRemoteGraphHandler, self).__init__(handler)
        @pdgd.util.SimpleHandler.command('TerminateRemoteGraph')
        def onTerminateRemoteGraph(self, params):
            printlog('Termination requested by remote, shutting down.')
            terminateEvent.set()
    pdgd.util.register_new_handler(TerminateRemoteGraphHandler, 'TerminateRemoteGraphHandler', 'remote_graph')

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cooks a TOP node as a single job")

    # Parameters that all calls must pass through
    parser.add_argument('--hip', default='', required=True,
        help="Full path to the .hip file containing the TOP to cook")
    parser.add_argument('--toppath', default='', required=True,
        help="The topnet path within the hip file, e.g. /obj/topnet1")
    # optional
    parser.add_argument('--report', choices=('all', 'none', 'outputnode', 'items'),
        default='none', help='What result data is forwarded from fetched node')
    parser.add_argument('--keepopen', choices=('error', 'always'),
        help='Keep hython running after the cook completes')    
    # NOTE: Only none and items are currently used, outputnode and all left for
    # backwards compatibility
    parser.add_argument('--fetch', action='store_true',
        help="PDG_ITEM_NAME is fetching workitem to set for topfetchinput")
    parser.add_argument('--outputdir', default='',
        help="Full path to directory for fetched output .json files")

    parser.add_argument('--taskgraphin', type=str,
        help='Path to the taskgraph file that should be restored')
    parser.add_argument('--taskgraphout', type=str,
        help='Path to the taskgraph file that should be written out after cook')

    parser.add_argument('--verbosity', type=int, default='1',
        help='Determines what node errors or warnings get printed to stdout '
             'after the top cook finishes. Setting verbosity=0 prints no '
             'messages, verbosity=1 prints just node errors and verbosity=2 '
             'prints both errors and warnings')

    # Data layer server parameters
    parser.add_argument('--enabledatalayerserver', action='store_true',
        help='[DEPRECATED] use --datalayerserverport')

    parser.add_argument('--datalayerserverport', type=int,
        help='Specifies the port to be used by the data layer'
             ' server.  0 means choose a free port.')
    parser.add_argument('--datalayerfile', type=str,
        help='Path to a file that data layer server info should should be'
             ' written to instead of stdout')

    args = parser.parse_args()

    # Set up PDGD
    terminate_event = threading.Event()
    if args.datalayerserverport is not None:
        _startDataLayerServerWithTerminate(args.datalayerserverport,
            terminate_event)

    # set the fetch input workitem file env var
    if args.fetch:
        os.environ['PDG_FETCH_JSON_FILE'] = pdgjson.getWorkItemJsonPath(
            os.environ['PDG_ITEM_NAME'])

    printlog("Running Houdini {} with PID {}".format(
        hou.applicationVersionString(), os.getpid()))

    hip_norm = args.hip.replace('\\', '/')
    hip_norm = hou.text.expandString(hip_norm)
    printlog("Loading .hip file %s." % hip_norm)
    if not hq.fileExists(hip_norm):
        printlog("Cannot find file %s" % hip_norm)
    # convert to forward slash again to handle any windows-style path components
    # that have been expanded
    hip_norm = hip_norm.replace('\\', '/')

    # Remove the working dir from sys.path to avoid overriding imports
    first_dir = sys.path.pop(0)
    hou.hipFile.load(hip_norm, ignore_load_warnings=True)
    sys.path.insert(0, first_dir)

    if args.taskgraphin:
        if not os.path.isfile(args.taskgraphin):
            print("Cannot find file %s" % args.taskgraphin)
            hou.exit(1)
    try:
        # FIXME: Disabled pending verification of deserialize and generation-after-deserialize
        # tasgraphin = args.taskgraphin
        taskgraphin = ''
        had_error, disp_node = cookTopNode(
            args.toppath, taskgraphin, verbosity=args.verbosity)
        pdg_node = disp_node.getPDGNode()
        if pdg_node:
            # save the task state if requested
            if args.taskgraphout:
                pdg_node.context.serializeWorkItems(args.taskgraphout, "")
                print("Saved task graph state to %s" % args.taskgraphout)
            if args.report != 'none':
                if args.outputdir:
                    data_dir = args.outputdir
                else:
                    data_dir = pdg_node.scheduler.workingDir(True)

                # prefix the item files with the fetching item name to ensure
                # uniqueness if the node is fetched multiple times in one cook
                fetching_item_name = os.environ.get('PDG_ITEM_NAME', '')
                if fetching_item_name:
                    item_prefix = '_' + fetching_item_name
                else:
                    item_prefix = ''

                serialize_workitems(pdg_node, item_prefix, data_dir)

                if args.report != 'items':
                    # Deprecated - results are in the item json and don't have to be
                    # reported explicitly
                    report_resultdata(pdg_node, args.report == 'all')
        
        if args.keepopen:
            if args.keepopen == 'always' or (
                args.keepopen == 'error' and had_error):
                printlog('Finished {}but keeping open. '
                         'Waiting on remote termination command, '
                         'or manual kill'.format(
                             'with error, ' if had_error else ''))
                
                while not terminate_event.is_set():
                    terminate_event.wait(0.33)

        if had_error:
            hou.exit(1)
    except:
        traceback.print_exc()
        hou.exit(1)
    hou.exit(0)
