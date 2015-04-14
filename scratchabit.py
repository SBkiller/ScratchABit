# ScratchABit - interactive disassembler
#
# Copyright (c) 2015 Paul Sokolovsky
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
import sys
import time
import re
import logging as log

import engine
import idaapi
import xtensa

import help


def disasm_one(p):
    insnsz = p.ana()
    p.out()
    print("%08x %s" % (p.cmd.ea, p.cmd.disasm))
    p.cmd.ea += p.cmd.size
    p.cmd.size = 0


import editorext as editor

HEIGHT = 25

class Editor(editor.EditorExt):

    def __init__(self, *args):
        super().__init__(*args)
        self.model = None
        self.addr_stack = []

    def set_model(self, model):
        self.model = model
        self.set_lines(model.lines())

    def show_line(self, l):
        if not isinstance(l, str):
            l = "%08x " % l.ea +  l.render()
        super().show_line(l)

    def goto_addr(self, to_addr, from_addr=None):
        t = time.time()
        model = engine.render_partial_around(to_addr, HEIGHT * 2)
        self.show_status("Rendering time: %fs" % (time.time() - t))
        self.set_model(model)

        no = self.model.addr2line_no(to_addr)
        if no is not None:
            if from_addr is not None:
                self.addr_stack.append(from_addr)
            self.goto_line(no)
        else:
            self.show_status("Unknown address: %x" % to_addr)

    def update_model(self):
        addr = self.cur_addr()
        t = time.time()
        model = engine.render_partial_around(addr, HEIGHT * 2)
        self.show_status("Rendering time: %fs" % (time.time() - t))
        self.set_model(model)
        self.cur_line = model.target_addr_lineno
        self.top_line = self.cur_line - self.row
        self.update_screen()

    def handle_cursor_keys(self, key):
        if super().handle_cursor_keys(key):
            #log.debug("handle_cursor_keys: cur: %d, total: %d", self.cur_line, self.total_lines)
            if self.cur_line <= HEIGHT or self.total_lines - self.cur_line <= HEIGHT:
                log.debug("handle_cursor_keys: triggering update")
                self.update_model()

            return True
        else:
            return False

    def cur_addr(self):
        line = self.get_cur_line()
        if isinstance(line, str):
            parts = line.split(None, 1)
            return int(parts[0], 16)
        return line.ea

    def analyze_status(self, cnt):
        self.show_status("Analyzing (%d insts so far)" % cnt)

    def handle_key(self, key):
        if key == editor.KEY_ENTER:
            line = self.get_cur_line()
            log.info("Enter pressed: %s" % line)
            self.show_status("Enter pressed: %s" % line)
            if isinstance(line, engine.DisasmObj):
                o = line.get_operand_addr()
                if o:
                    self.goto_addr(o.addr, from_addr=line.ea)
        elif key == editor.KEY_ESC:
            if self.addr_stack:
                self.show_status("Returning")
                self.goto_addr(self.addr_stack.pop())
        elif key == b"q":
            return editor.KEY_QUIT
        elif key == b"c":
            addr = self.cur_addr()
            self.show_status("Analyzing at %x" % addr)
            engine.add_entrypoint(addr)
            engine.analyze(self.analyze_status)
            self.update_model()
        elif key == b"d":
            addr = self.cur_addr()
            fl = self.model.AS.get_flags(addr)
            if fl not in (self.model.AS.DATA, self.model.AS.UNK):
                self.show_status("Undefine first")
                return
            if fl == self.model.AS.UNK:
                self.model.AS.set_flags(addr, 1, self.model.AS.DATA, self.model.AS.DATA_CONT)
            else:
                sz = self.model.AS.get_unit_size(addr)
                self.model.undefine(addr)
                sz *= 2
                if sz > 4: sz = 1
                self.model.AS.set_flags(addr, sz, self.model.AS.DATA, self.model.AS.DATA_CONT)
            self.update_model()
        elif key == b"u":
            addr = self.cur_addr()
            self.model.undefine(addr)
            self.update_model()
        elif key == b"o":
            addr = self.cur_addr()
            line = self.get_cur_line()
            o = line.get_operand_addr()
            if self.model.AS.get_arg_prop(addr, o.n, "type") == idaapi.o_mem:
                self.model.AS.set_arg_prop(addr, o.n, "type", idaapi.o_imm)
            else:
                self.model.AS.set_arg_prop(addr, o.n, "type", idaapi.o_mem)
                label = self.model.AS.get_label(o.addr)
                if not label:
                    self.model.AS.make_label(None, o.addr)
            self.update_model()
        elif key == b"n":
            addr = self.cur_addr()
            label = self.model.AS.get_label(addr)
            s = label or self.model.AS.get_default_label(addr)
            res = self.dialog_edit_line(line=s)
            if res:
                self.model.AS.set_label(addr, res)
                if not label:
                    # If it's new label, we need to add it to model
                    self.model.insert_vline(self.cur_line, addr, engine.Label(addr))
            self.update_screen()
        elif key == b"g":
            res = self.dialog_edit_line(line="")
            if res:
                self.goto_addr(int(res, 0), from_addr=self.cur_addr())
            else:
                self.update_screen()
        elif key == editor.KEY_F1:
            help.help(self)
            self.update_screen()
        elif key == b"S":
            save_state()
            self.show_status("Saved.")


def parse_disasm_def(fname):
    with open(fname) as f:
        for l in f:
            l = re.sub(r"#.*$", "", l)
            l = l.strip()
            if not l:
                continue
            #print(l)
            if l.startswith("load"):
                args = l.split()
                addr = int(args[2], 0)
                engine.ADDRESS_SPACE.load_content(addr, open(args[1], "rb"))
                print("Loading %s @0x%x" % (args[1], addr))
            else:
                if "(" in l:
                    m = re.match(r"(.+?)\s*\(\s*(.+?)\s*\)\s+(.+)", l)
                    #print(m.groups())
                    start = int(m.group(1), 0)
                    end = start + int(m.group(2), 0) - 1
                    props = m.group(3)
                else:
                    m = re.match(r"(.+?)\s*-\s*(.+?)\s+(.+)", l)
                    #print(m.groups())
                    start = int(m.group(1), 0)
                    end = int(m.group(2), 0)
                    props = m.group(3)
                a = engine.ADDRESS_SPACE.add_area(start, end, props.upper())
                print("Adding area: %s" % engine.str_area(a))


def save_state():
    with open("project.labels", "w") as f:
        engine.ADDRESS_SPACE.save_labels(f)
    with open("project.args", "w") as f:
        engine.ADDRESS_SPACE.save_arg_props(f)
    with open("project.aspace", "w") as f:
        engine.ADDRESS_SPACE.save_areas(f)


if __name__ == "__main__":
    parse_disasm_def(sys.argv[1])
    log.basicConfig(filename="scratchabit.log", format='%(asctime)s %(message)s', level=log.DEBUG)
    log.info("Started")

    p = xtensa.PROCESSOR_ENTRY()
    engine.set_processor(p)

    entry = 0x40000080

    engine.add_entrypoint(entry)
    engine.add_entrypoint(0x40000010)
    #engine.add_entrypoint(0x40000020)
    #engine.add_entrypoint(0x40000030)
    #engine.add_entrypoint(0x40000050)
    #engine.add_entrypoint(0x40000070)
    engine.analyze()

    #engine.print_address_map()

    t = time.time()
    #_model = engine.render()
    _model = engine.render_partial_around(entry, HEIGHT * 2)
    print("Rendering time: %fs" % (time.time() - t))
    #print(_model.lines())
    #sys.exit()

    e = Editor(1, 1, 78, 23)
    e.init_tty()
    e.cls()
    e.enable_mouse()
    e.draw_box(0, 0, 80, 25)
    e.set_model(_model)
    e.goto_addr(entry)
    e.loop()
    e.deinit_tty()
