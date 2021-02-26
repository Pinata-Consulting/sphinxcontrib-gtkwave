import subprocess
from docutils.parsers.rst import directives
from easyprocess import EasyProcess
from pyvirtualdisplay.smartdisplay import SmartDisplay, DisplayTimeoutError
from PIL import ImageFilter
import docutils.parsers.rst.directives.images
import logging
import os
import pathlib
import tempfile
from docutils import nodes
import pickle
import re

import hashlib
"""
    sphinxcontrib.gtkwave
    ================================

    This extension provides a directive to include the screenshot of GtkWave as
    image while building the docs.

"""

__version__ = '0.1'

log = logging.getLogger(__name__)
log.debug('sphinxcontrib.gtkwave (version:%s)' % __version__)


class GtkwaveError(Exception):
    pass


tcl = r'''
set clk48 [list]

set nfacs [ gtkwave::getNumFacs ]
for {set i 0} {$i < $nfacs } {incr i} {
set facname [ gtkwave::getFacName $i ]

# set fields [split $facname "\\"]
# set sig [ lindex $fields 1 ]
set fields [split $facname "\\"]
set sig1 [ lindex $fields 0 ]
set sig2 [ lindex $fields 1 ]
if {[llength $fields]  == 2} {
set sig "$sig2"
} else {
set sig "$sig1"
}

lappend clk48 "$sig"
}

set num_added [ gtkwave::addSignalsFromList $clk48 ]

set max_time [ gtkwave::getMaxTime ]
set min_time [ gtkwave::getMinTime ]

gtkwave::setZoomRangeTimes $min_time $max_time
'''

tcl_with_gtkw = r'''
gtkwave::setLeftJustifySigs on
'''

rc = r'''
hide_sst 1
splash_disable 1
enable_vert_grid 0
ignore_savefile_pos 1
'''


def get_src(self):
    return self.state_machine.get_source(self.lineno)


def get_black_box(im):
    im3 = im.point(lambda x: 255 * bool(x))
    im2 = im3.filter(ImageFilter.MaxFilter(3))
    im5 = im2.point(lambda x: 255 * bool(not x))
    bbox = im5.getbbox()
    # ignore_black_parts
    im6 = im.crop(bbox)
    bbox2 = im6.getbbox()
    if bbox and bbox2:
        bbox3 = (
            bbox[0] + bbox2[0],
            bbox[1] + bbox2[1],
            bbox[0] + bbox2[2],
            bbox[1] + bbox2[3],
        )
        return bbox3


def prog_shot(cmd, f, wait, timeout, screen_size, visible, bgcolor):
    '''start process in headless X and create screenshot after 'wait' sec.
    Repeats screenshot until it is not empty if 'repeat_if_empty'=True.

    wait: wait at least N seconds after first window is displayed,
    it can be used to skip splash screen

    :param wait: int
    '''
    disp = SmartDisplay(visible=visible, size=screen_size, bgcolor=bgcolor)
    proc = EasyProcess(cmd)

    def cb_imgcheck(img):
        """accept img if height > minimum."""
        rec = get_black_box(img)
        if not rec:
            return False
        left, upper, right, lower = rec
        accept = lower - upper > 30  # pixel
        log.debug('cropped img size=' + str((left, upper, right, lower)) +
                  ' accepted=' + str(accept))
        return accept

    with  SmartDisplay(visible=visible, size=screen_size, bgcolor=bgcolor) as disp:
     with EasyProcess(cmd) as proc:
    # def func():
        if wait:
            proc.sleep(wait)
        try:
            img = disp.waitgrab(timeout=timeout, cb_imgcheck=cb_imgcheck)
        except DisplayTimeoutError as e:
            if not proc.is_alive():
                print("gtkwave stderr: " + proc.stderr)
                print("gtkwave stdout: " + proc.stdout)
            raise DisplayTimeoutError(str(e) + ' ' + str(proc))
        # return img

    if img:
        bbox = get_black_box(img)
        assert bbox
        # extend to the left side
        bbox = (0, bbox[1], bbox[2], bbox[3])
        img = img.crop(bbox)

        img.save(f)
    return (proc.stdout, proc.stderr)


parent = docutils.parsers.rst.directives.images.Image
image_id = 0


class GtkwaveDirective(parent):
    option_spec = parent.option_spec.copy()
    option_spec.update(
        dict(
            #                       prompt=directives.flag,
            screen=directives.unchanged,
            wait=directives.nonnegative_int,
            #                       stdout=directives.flag,
            #                       stderr=directives.flag,
            visible=directives.flag,
            timeout=directives.nonnegative_int,
            bgcolor=directives.unchanged,
        ))

    def run(self):
        '''Collect information, but do not generate files here'''
        vcd = [
            self.state.document.settings.env.relfn2path(f)[1]
            for f in self.arguments[0].split()
        ]

        node = gtkwave()

        screen = self.options.get('screen', '1024x768')
        screen = tuple(map(int, screen.split('x')))
        wait = self.options.get('wait', 1)
        timeout = self.options.get('timeout', 12)
        bgcolor = self.options.get('bgcolor', 'white')
        visible = 'visible' in self.options

        node['screen'] = screen
        node['wait'] = wait
        node['timeout'] = timeout
        node['bgcolor'] = bgcolor
        node['visible'] = visible

        node['vcd'] = vcd

        return [node]


class GtkwaveBuilder(object):
    def __init__(self, builder):
        self.builder = builder


def _on_builder_inited(app):
    app.builder.gtkwave_builder = GtkwaveBuilder(app.builder)


def hash_gtkwave_node(node):
    h = hashlib.sha1()
    # may include different file relative to doc
    h.update(pickle.dumps(node['vcd']))
    h.update(b'\0')
    #h.update(node['uml'].encode('utf-8'))
    return h.hexdigest()


def generate_name(self, node, fileformat):
    key = hash_gtkwave_node(node)
    fname = 'gtkwave-%s.%s' % (key, fileformat)
    imgpath = getattr(self.builder, 'imgpath', None)
    if imgpath:
        return ('/'.join((self.builder.imgpath, fname)),
                os.path.join(self.builder.outdir, '_images', fname))
    else:
        return fname, os.path.join(self.builder.outdir, fname)


def _get_png_tag(self, fnames, node):
    refname, outfname = fnames['png']
    alt = node.get('alt', " ".join(node['vcd']))

    return ('<img src="%s" alt="%s"/>\n' % (self.encode(refname),
                                            self.encode(alt)))


def render_gtkwave(self, node):
    # TODO use the caching capability in Sphinx to avoid re-rendering image
    # put node representing rendered image
    refname, outfname = generate_name(self, node, "png")

    vcd = node['vcd']

    with tempfile.NamedTemporaryFile(
            prefix='gtkwave', suffix='.tcl', delete=0) as tclfile:
        tclfile.write((tcl
                       if len(vcd) == 1 else tcl_with_gtkw).encode('utf-8'))

    with tempfile.NamedTemporaryFile(
            prefix='gtkwave', suffix='.rc', delete=0) as rcfile:
        rcfile.write(rc.encode('utf-8'))

    cmd = ['gtkwave'] + vcd + [
        '--tcl_init',
        tclfile.name,
        '--rcfile',
        rcfile.name,
        '--nomenu',
    ]

    print("running:" + subprocess.list2cmdline(cmd))

    prog_shot(
        cmd,
        outfname,
        screen_size=node['screen'],
        wait=node['wait'],
        timeout=node['timeout'],
        visible=node['visible'],
        bgcolor=node['bgcolor'])

    # if the build fails, we never reach this point and we can re-run the logged command line.
    os.remove(tclfile.name)
    os.remove(rcfile.name)

    return (refname, outfname)


def html_visit_gtkwave(self, node):
    refname, outfname = render_gtkwave(self, node)

    fnames = {"png": (refname, outfname)}

    self.body.append(self.starttag(node, 'p', CLASS='plantuml'))
    self.body.append(_get_png_tag(self, fnames, node))
    self.body.append('</p>\n')

    # rep = nodes.image(uri=outfname)
    # node.parent.replace(node, rep)
    raise nodes.SkipNode


def latex_visit_gtkwave(self, node):
    refname, outfname = render_gtkwave(self, node)

    # put node representing rendered image
    img_node = nodes.image(uri=refname, **node.attributes)
    img_node.delattr('uml')
    node.append(img_node)


def latex_depart_gtkwave(self, node):
    pass


_NODE_VISITORS = {
    'html': (html_visit_gtkwave, None),
    'latex': (latex_visit_gtkwave, latex_depart_gtkwave)
}


class gtkwave(nodes.General, nodes.Element):
    pass


def setup(app):
    app.add_node(gtkwave, **_NODE_VISITORS)
    app.add_directive('gtkwave', GtkwaveDirective)
    app.connect('builder-inited', _on_builder_inited)
