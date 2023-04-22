#!/usr/bin/env python
#
# Copyright (c) 2015 Dale Sedivec
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

import abc
import argparse
import binascii
import collections
import logging
import math
import os
import os.path
import shutil
import sys
import tempfile
import typing as t
import zipfile
import zlib
from io import StringIO
from xml.etree import ElementTree

try:
    import py7zlib
except ImportError:
    py7zlib = None


# Used in a few places to determine how much we read at a time.
READ_SIZE = 65536


# Limit for the file size we'll read into memory.  Seems like 4 MiB is
# a good limit for reading ROMs.  I'm not even exactly sure how we'd
# represent 4 MiB with iNES headers (as documented at the below link).
# Maybe PlayChoice-10?  Of course, even 4 MiB is laughable on any
# modern system.
#
# http://wiki.nesdev.com/w/index.php/Myths#Largest_game
MAX_MEMORY_FILE_SIZE = 4 * 2**20


class SkipROM(Exception):
    """Indicates that we found a ROM format we can't parse.

    For example, NES 2.0 is not implemented as of this writing.

    """

    pass


MIN_HEADER_READ_SIZE = 16


def parse_header(header_bytes: bytes):
    assert len(header_bytes) >= MIN_HEADER_READ_SIZE, len(header_bytes)
    if header_bytes.startswith(b"UNIF"):
        raise SkipROM("UNIF currently unsupported")
    if not header_bytes.startswith(b"NES\x1a"):
        return None
    if header_bytes[7] & 0xC == 8:
        raise SkipROM("NES 2.0 currently unsupported")
    if header_bytes[6] & 4:
        raise SkipROM('ROMs with "trainers" currently unsupported')
    return ROMInfo(
        prg_rom_size=header_bytes[4] * 16384,
        chr_rom_size=header_bytes[5] * 8192,
        mapper=(header_bytes[7] & 0xF0) + ((header_bytes[6] & 0xF0) >> 4),
        mirroring=header_bytes[6] & 9,
        has_trainer=bool(header_bytes[6] & 4),
        has_nvram=bool(header_bytes[6] & 2),
        is_playchoice_10=bool(header_bytes[7] & 2),
        is_vs_unisystem=bool(header_bytes[7] & 1),
        prg_ram_size=header_bytes[8] * 8192,
        tv_system=header_bytes[9] & 1,
    )


class FileInfo:
    def __init__(self, name, crc32, rom_info, handler_data=None):
        self.name = name
        self.crc32 = crc32
        self.rom_info = rom_info
        self.handler_data = handler_data


class IOHandler(abc.ABC):
    @abc.abstractmethod
    def __init__(self, file_path: str):
        raise NotImplementedError

    @abc.abstractmethod
    def __iter__(self):
        raise NotImplementedError

    def _make_file_info(self, name: str, file_obj, handler_data=None):
        header_bytes = file_obj.read(MIN_HEADER_READ_SIZE)
        try:
            rom_info = parse_header(header_bytes)
        except SkipROM as ex:
            logging.warning("%s: %s", name, ex)
            raise
        if rom_info:
            crc32 = 0
        else:
            crc32 = binascii.crc32(header_bytes)
        while True:
            data = file_obj.read(READ_SIZE)
            if not data:
                break
            crc32 = binascii.crc32(data, crc32)
        return FileInfo(
            name,
            "%08X" % (crc32 & 0xFFFFFFFF,),
            rom_info,
            handler_data=handler_data,
        )


class WritableIOHandler(IOHandler):
    def update(self, requests):
        raise NotImplementedError

    def _update_file(self, path, requests):
        # Note: There is currently no case I know of where we'd want
        # more than one request for a given file.  This design just
        # leaves the door open for some future functionality.
        for request in requests:
            header_bytes = make_ines_header(request.rom_info)
            if request.type == UpdateRequest.REQ_HEADER_REPLACE:
                with open(path, "r+b") as the_file:
                    the_file.write(header_bytes)
            elif request.type == UpdateRequest.REQ_HEADER_INSERT:
                temp_file = None
                try:
                    temp_file = tempfile.NamedTemporaryFile(
                        dir=os.path.dirname(path), delete=False
                    )
                    temp_file.write(header_bytes)
                    with open(path, "rb") as orig_file:
                        shutil.copyfileobj(orig_file, temp_file)
                    temp_file.close()
                    os.rename(temp_file.name, path)
                finally:
                    if temp_file and os.path.exists(temp_file.name):
                        if not temp_file.closed:
                            temp_file.close()
                        os.unlink(temp_file.name)
            else:
                raise Exception("unknown request type %r" % (request.type,))


class UpdateRequest:
    REQ_HEADER_INSERT = 1
    REQ_HEADER_REPLACE = 2

    def __init__(self, file_info, req_type, rom_info):
        self.file_info = file_info
        self.type = req_type
        self.rom_info = rom_info


class FileIOHandler(WritableIOHandler):
    def __init__(self, path):
        self._path = path

    def __iter__(self):
        with open(self._path, "rb") as the_file:
            try:
                file_info = self._make_file_info(self._path, the_file)
            except SkipROM:
                return
        yield file_info

    def update(self, requests):
        for request in requests:
            if request.file_info.name != self._path:
                raise Exception(
                    "can only accept requests for %r, not for %r"
                    % (self._path, request.file_info.name)
                )
        self._update_file(self._path, requests)


class ZipIOHandler(WritableIOHandler):
    def __init__(self, path):
        self._path = path
        self._updates = 0

    def __iter__(self):
        expected_updates = self._updates
        with zipfile.ZipFile(self._path, "r") as zip_file:
            for name in zip_file.namelist():
                if self._updates != expected_updates:
                    raise Exception(
                        "iterator invalidated by concurrent modification"
                    )
                full_name = os.path.join(self._path, name)
                try:
                    yield self._make_file_info(
                        full_name, zip_file.open(name), name
                    )
                except (zipfile.BadZipfile, zlib.error) as ex:
                    logging.warning(
                        "can't read %s within %s: %s", name, self._path, ex
                    )
                except SkipROM:
                    pass

    def update(self, requests):
        requests_by_file = collections.defaultdict(list)
        for request in requests:
            requests_by_file[request.file_info.handler_data].append(request)
        temp_file = None
        temp_dir = None
        try:
            temp_file = tempfile.NamedTemporaryFile(
                dir=os.path.dirname(self._path), delete=False
            )
            temp_dir = tempfile.mkdtemp()
            with zipfile.ZipFile(
                self._path, "r"
            ) as orig_zip_file, zipfile.ZipFile(temp_file, "w") as new_zip_file:
                for name in orig_zip_file.namelist():
                    member_path = orig_zip_file.extract(name, temp_dir)
                    requests = requests_by_file.pop(name, ())
                    self._update_file(member_path, requests)
                    new_zip_file.write(member_path, name, zipfile.ZIP_DEFLATED)
                    os.unlink(member_path)
            if requests_by_file:
                print(repr(requests))
                raise Exception(
                    "requests for unknown files: %s"
                    % (", ".join(requests_by_file),)
                )
            temp_file.close()
            os.rename(temp_file.name, self._path)
            self._updates += 1
        finally:
            if temp_file:
                if os.path.exists(temp_file.name):
                    if not temp_file.closed:
                        temp_file.close()
                    os.remove(temp_file.name)
                if temp_dir and os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir, True)


class SevenZipIOHandler(IOHandler):
    def __init__(self, path):
        self._path = path

    def __iter__(self):
        with open(self._path, "rb") as sz_file:
            assert py7zlib is not None
            archive = py7zlib.Archive7z(sz_file)
            for member in archive.files:
                if member.size > MAX_MEMORY_FILE_SIZE:
                    logging.warning(
                        "skipping %s in %s: too big (%d bytes)",
                        member.filename,
                        self._path,
                        member.size,
                    )
                    continue
                contents = StringIO(member.read())
                full_name = os.path.join(self._path, member.filename)
                try:
                    yield self._make_file_info(full_name, contents)
                except SkipROM:
                    pass


EXTENSION_TO_IO_HANDLER: dict[str, t.Type[IOHandler]] = {
    ".zip": ZipIOHandler,
}
if py7zlib:
    EXTENSION_TO_IO_HANDLER[".7z"] = SevenZipIOHandler


def format_kib(value: int):
    if value % 1024:
        kib = "%f KiB" % (value / 1024.0,)
    else:
        kib = "%d KiB" % (value // 1024,)
    return kib


def format_chr_rom(value: int):
    if value == 0:
        return "CHR RAM"
    else:
        return format_kib(value)


class ROMInfo:
    MIRROR_HORIZONTAL = 0
    MIRROR_VERTICAL = 1
    MIRROR_FOUR_SCREEN = 8
    MIRROR_FOUR_SCREEN_ODD = 9
    # Signifies mapper-controlled mirroring, where the low bit in the
    # iNES flags 6 header field is ignored.  We just guess that it's
    # mapper-controlled if the cartridge DB doesn't obviously indicate
    # horizontal or vertical mirroring (nor four screen mirroring).
    #
    # This value chosen so that it turns into MIRROR_HORIZONTAL (but
    # remember: ignored) if/when written to an iNES header.
    MIRROR_CONTROLLED = 16

    MIRROR_LABELS = {
        MIRROR_HORIZONTAL: "Horizontal",
        MIRROR_VERTICAL: "Vertical",
        MIRROR_FOUR_SCREEN: "Four screen",
        MIRROR_FOUR_SCREEN_ODD: "Four screen (odd)",
        MIRROR_CONTROLLED: "Mapper controlled",
    }

    TV_NTSC = 0
    TV_PAL = 1
    # Sometimes the same ROM is used for both TV systems, as far as I
    # can tell.
    #
    # This value chosen so that it turns into TV_NTSC if/when written
    # to an iNES header.  See comment in make_ines_header.
    TV_BOTH = 2

    TV_LABELS = {
        TV_NTSC: "NTSC",
        TV_PAL: "PAL",
        TV_BOTH: "NTSC and PAL",
    }

    FIELD_LABELS = dict(
        (
            ("prg_rom_size", "PRG ROM"),
            ("prg_ram_size", "PRG RAM"),
            ("chr_rom_size", "CHR ROM"),
            ("mapper", "Mapper"),
            ("mirroring", "Mirroring"),
            ("tv_system", "TV System"),
            ("has_nvram", "Has NVRAM"),
            ("has_trainer", "Has Trainer"),
            ("is_playchoice_10", "Is PlayChoice-10"),
            ("is_vs_unisystem", "Is VS. UniSystem"),
        )
    )

    FIELD_FORMATTERS = {
        "prg_rom_size": format_kib,
        "prg_ram_size": format_kib,
        "chr_rom_size": format_chr_rom,
        "mirroring": MIRROR_LABELS.get,
        "tv_system": TV_LABELS.get,
    }

    FIELDS = tuple(FIELD_LABELS)

    def __init__(
        self,
        prg_rom_size,
        prg_ram_size,
        chr_rom_size,
        mapper,
        mirroring,
        tv_system,
        has_nvram,
        has_trainer,
        is_playchoice_10,
        is_vs_unisystem,
    ):
        self.prg_rom_size = prg_rom_size
        self.prg_ram_size = prg_ram_size
        self.chr_rom_size = chr_rom_size
        self.mapper = mapper
        self.mirroring = mirroring
        self.tv_system = tv_system
        self.has_nvram = has_nvram
        self.has_trainer = has_trainer
        self.is_playchoice_10 = is_playchoice_10
        self.is_vs_unisystem = is_vs_unisystem

    def _is_ignored_mirroring_diff(
        self, mirror_val_a: int, mirror_val_b: int
    ) -> bool:
        # Mapper-controlled mirroring ignores the low bit, which is
        # H/V mirroring.  Therefore if one mirroring value is
        # CONTROLLED, and neither is FOUR_SCREEN, then we can ignore
        # the difference in mirroring values.
        return (
            (mirror_val_a | mirror_val_b)
            & (self.MIRROR_CONTROLLED | self.MIRROR_FOUR_SCREEN)
        ) == self.MIRROR_CONTROLLED

    def diff(self, other: "ROMInfo") -> dict[str, tuple]:
        differences = {}
        for attr in self.FIELDS:
            self_val = getattr(self, attr)
            other_val = getattr(other, attr)
            if self_val != other_val and not (
                (
                    attr == "mirroring"
                    and self._is_ignored_mirroring_diff(self_val, other_val)
                )
                or (
                    attr == "tv_system"
                    and self.TV_BOTH in (self_val, other_val)
                )
            ):
                differences[attr] = (self_val, other_val)
        return differences


def visit_roms(rom_paths: t.Iterable[str], visitor, *args, **kwargs):
    for rom_path in rom_paths:
        extension = os.path.splitext(rom_path)[1]
        io_handler_cls = EXTENSION_TO_IO_HANDLER.get(
            extension.lower(), FileIOHandler
        )
        io_handler = io_handler_cls(rom_path)
        requests = []
        for file_info in io_handler:
            request = visitor(file_info, *args, **kwargs)
            if request:
                requests.append(request)
        if requests:
            try:
                update = io_handler.update
            except AttributeError:
                logging.warning("cannot update file of this type: %s", rom_path)
            else:
                update(requests)


def make_ines_header(rom_info: ROMInfo) -> bytes:
    if (
        rom_info.prg_rom_size & 16383
        or rom_info.prg_rom_size > (0xFF * 16384)
        or rom_info.prg_rom_size < 0
    ):
        raise Exception(
            "can't represent PRG ROM size %d in iNES header"
            % (rom_info.prg_rom_size,)
        )
    prg_rom = rom_info.prg_rom_size // 16384
    if (
        rom_info.chr_rom_size & 8191
        or rom_info.chr_rom_size > (0xFF * 8192)
        or rom_info.chr_rom_size < 0
    ):
        raise Exception(
            "can't represent CHR ROM size %d in iNES header"
            % (rom_info.chr_rom_size,)
        )
    chr_rom = rom_info.chr_rom_size // 8192
    if rom_info.mapper < 0 or rom_info.mapper > 255:
        raise Exception(
            "can't represent mapper %d in iNES header" % (rom_info.mapper,)
        )
    flags_6 = (
        ((rom_info.mapper & 0xF) << 4)
        |
        # Mask of 0xF turns MIRROR_CONTROLLED into 0
        # (technically MIRROR_HORIZONTAL, but hopefully ignored
        # entirely by the mapper).
        (rom_info.mirroring & 0xF)
        | (rom_info.has_trainer << 2)
        | (rom_info.has_nvram << 1)
    )
    flags_7 = (
        (rom_info.mapper & 0xF0)
        | (rom_info.is_playchoice_10 << 1)
        | rom_info.is_vs_unisystem
    )
    if rom_info.prg_ram_size > (0xFF * 8192) or rom_info.prg_ram_size < 0:
        raise Exception(
            "can't represent PRG RAM size %d in iNES header"
            % (rom_info.prg_ram_size,)
        )
    # Crisis Force, for example, has a 2 KiB PRG RAM, which can't be
    # represented in iNES's increments of 8 KiB.  I'm going to go
    # ahead and round up to the nearest 8 KiB, in hopes that's better
    # than just writing e.g. 0 here.
    prg_ram = int(math.ceil(rom_info.prg_ram_size / 8192.0))
    # If the same ROM exists for both NTSC and PAL we'll just default
    # to NTSC because because NTSC seems more common (NTSC NES,
    # Famicom, PlayChoice-10, VS. UniSystem) than PAL.  This mask,
    # along with the purposely chosen values for these constants,
    # accomplishes exactly this.
    flags_9 = rom_info.tv_system & 1
    header = bytearray(b"NES\x1a")
    header.extend(
        (
            prg_rom,
            chr_rom,
            flags_6,
            flags_7,
            prg_ram,
            flags_9,
        )
    )
    return header


def parse_size(str_value: str) -> int:
    if not str_value.endswith("k") or not str_value[:-1].isdigit():
        raise Exception("can't parse size %r" % (str_value,))
    return int(str_value[:-1]) * 2**10


def etree_find_one(elem, path: str, optional=False):
    children = elem.findall(path)
    if len(children) > 1:
        raise Exception(
            "too many %r children of %s" % (path, ElementTree.tostring(elem))
        )
    if not optional and len(children) == 0:
        raise Exception(
            "no %r children of %s" % (path, ElementTree.tostring(elem))
        )
    return children[0] if children else None


FOUR_SCREEN_BOARDS = frozenset(
    [
        # Gauntlet
        "NES-DRROM",
        "NES-TR1ROM",
        "TENGEN-800004",
        # Rad Racer II
        "NES-TVROM",
        # Napoleon Senki
        "IREM-74*161/161/21/138",
        # May not exist, but are in Nestopia's source
        "HVC-DRROM",
        "HVC-TVROM",
    ]
)


# Dendy is close enough to PAL, I think?
PAL_SYSTEMS = frozenset(["NES-PAL", "NES-PAL-A", "NES-PAL-B", "Dendy"])


def parse_db_entry(elem: ElementTree.Element) -> tuple[str, ROMInfo]:
    crc32 = elem.attrib["crc"].upper()
    system = elem.attrib["system"]
    tv_system = ROMInfo.TV_PAL if system in PAL_SYSTEMS else ROMInfo.TV_NTSC
    board = etree_find_one(elem, "board")
    if board is None:
        raise Exception("Can't find board element")
    mapper = int(board.attrib.get("mapper", 0))
    prg_rom_size = sum(
        parse_size(prg.attrib["size"]) for prg in board.findall("prg")
    )
    prg_ram_size = sum(
        parse_size(wram.attrib["size"]) for wram in board.findall("wram")
    )
    chr_rom_size = sum(
        parse_size(chr.attrib["size"]) for chr in board.findall("chr")
    )
    has_nvram = board.find(".//*[@battery='1']") is not None
    pad = etree_find_one(board, "pad", optional=True)
    if pad is None:
        pad_h = pad_v = None
    else:
        pad_h = int(pad.attrib.get("h", "0"))
        pad_v = int(pad.attrib.get("v", "0"))
        if pad_h and pad_v:
            raise Exception("both H and V solder pads set on %s" % (crc32,))
        if not (pad_h or pad_v):
            raise Exception(
                "neither H nor V set on pad element of %s" % (crc32,)
            )
    board_type = board.attrib.get("type")
    if board_type in FOUR_SCREEN_BOARDS:
        mirroring = ROMInfo.MIRROR_FOUR_SCREEN
        if pad and (pad_h or pad_v):
            raise Exception(
                "H and/or V pads set on four screen mirroring game %s"
                % (crc32,)
            )
    elif pad is None:
        # I've decided that no <pad> on a non-four-screen-mirroring
        # board means that mirroring must be mapper-controlled.
        mirroring = ROMInfo.MIRROR_CONTROLLED
    elif pad_h:
        mirroring = ROMInfo.MIRROR_VERTICAL
    elif pad_v:
        mirroring = ROMInfo.MIRROR_HORIZONTAL
    else:
        raise Exception("should never get here")
    return crc32, ROMInfo(
        prg_rom_size,
        prg_ram_size,
        chr_rom_size,
        mapper,
        mirroring,
        tv_system,
        has_nvram,
        0,
        system.lower() == "playchoice-10",
        system.lower() == "vs-unisystem",
    )


def load_db(path: str) -> dict[str, ROMInfo]:
    db = {}
    xml_iter = iter(
        ElementTree.iterparse(
            path or "NstDatabase.xml", events=("start", "end")
        )
    )
    event, root = next(xml_iter)
    for event, elem in xml_iter:
        if event == "end" and elem.tag in ("cartridge", "arcade"):
            crc32, rom_info = parse_db_entry(elem)
            existing_rom_info = db.get(crc32)
            if existing_rom_info:
                # XXX BUG: if PRG RAM is not a multiple of 8192 bytes,
                # we'll report a difference against the header which
                # is irrelevant because it cannot be corrected.  Not
                # sure how to fix this right now.
                differences = existing_rom_info.diff(rom_info)
                # Some ROMs are apparently identical between TV
                # systems (PAL vs. NTSC).  That's what we have
                # ROMInfo.TV_BOTH for.
                existing_tv_system = differences.pop("tv_system", None)
                if differences:
                    logging.warning(
                        (
                            "multiple different database entries for"
                            " CRC %s, ignoring differing entries"
                            " after the first"
                        ),
                        crc32,
                    )
                # If we already wrote TV_BOTH then that means we've
                # seen both PAL and NTSC for this ROM, so this third
                # one must be a duplicate.
                elif (
                    existing_tv_system == ROMInfo.TV_BOTH
                ) or rom_info.tv_system == existing_tv_system:
                    logging.warning(
                        "duplicate identical entries for CRC %s", crc32
                    )
                else:
                    # "Upgrade" the existing entry to TV_BOTH, since
                    # we've now seen both TV types for this ROM.
                    existing_rom_info.tv_system = ROMInfo.TV_BOTH
            else:
                db[crc32] = rom_info
            # Free memory
            elem.clear()
        elif event == "end" and elem.tag == "game":
            # Free more memory
            root.clear()
    return db


def cmd_read(args):
    file_info_line = "{file_info.name} ({file_info.crc32}):"
    template_lines = [file_info_line]
    max_label_len = max(len(label) for label in ROMInfo.FIELD_LABELS.values())
    for attr, label in ROMInfo.FIELD_LABELS.items():
        template_lines.append(
            "\t%-*s: {formatted_rom_values[%s]}" % (max_label_len, label, attr)
        )
    formatters = tuple(
        (attr, ROMInfo.FIELD_FORMATTERS.get(attr, str))
        for attr in ROMInfo.FIELDS
    )
    template = "\n".join(template_lines)

    def print_rom_info(file_info):
        if file_info.rom_info:
            formatted_rom_values = {
                attr: formatter(getattr(file_info.rom_info, attr))
                for attr, formatter in formatters
            }
            print(
                template.format(
                    file_info=file_info,
                    formatted_rom_values=formatted_rom_values,
                )
            )
        else:
            print(file_info_line.format(file_info=file_info), "no header")

    visit_roms(args.roms, print_rom_info)


def cmd_write(args):
    db = load_db(args.db)
    file_info_line = "{file_info.name} ({file_info.crc32}): {0}"

    def update_rom_header(file_info):
        db_rom_info = db.get(file_info.crc32)
        if not file_info.rom_info and not db_rom_info:
            print(
                file_info_line.format(
                    "no header, not in database, cannot add header",
                    file_info=file_info,
                )
            )
            return None
        elif not db_rom_info:
            print(
                file_info_line.format(
                    "not in database, skipping", file_info=file_info
                )
            )
            return None
        elif not file_info.rom_info:
            print(
                file_info_line.format(
                    "no header, will add header", file_info=file_info
                )
            )
            if args.dry_run:
                return None
            else:
                return UpdateRequest(
                    file_info, UpdateRequest.REQ_HEADER_INSERT, db_rom_info
                )
        diff = db_rom_info.diff(file_info.rom_info)
        if not diff:
            print(
                file_info_line.format(
                    "header matches database", file_info=file_info
                )
            )
        else:
            print(
                file_info_line.format(
                    "header differs from database, will update header",
                    file_info=file_info,
                )
            )
            for attr, (db_val, header_val) in diff.items():
                formatter = ROMInfo.FIELD_FORMATTERS.get(attr, str)
                print(
                    "\t%s: expected %s, read %s"
                    % (
                        ROMInfo.FIELD_LABELS[attr],
                        formatter(db_val),
                        formatter(header_val),
                    )
                )
            if args.dry_run:
                return None
            else:
                return UpdateRequest(
                    file_info, UpdateRequest.REQ_HEADER_REPLACE, db_rom_info
                )

    visit_roms(args.roms, update_rom_header)


def main():
    logging.basicConfig()
    parser = argparse.ArgumentParser()
    roms_help = "ROM files, or archives containing ROMs"
    # XXX Currently nothing logs at anything other than warn.  See
    # commented code below, too.
    # parser.add_argument("--verbose", "-v", default=False, action="store_true")
    subparsers = parser.add_subparsers(required=True)
    read_parser = subparsers.add_parser("read", help="read iNES headers")
    read_parser.set_defaults(handler=cmd_read)
    read_parser.add_argument("roms", nargs="+", metavar="rom", help=roms_help)
    write_parser = subparsers.add_parser(
        "write", help="add/correct iNES headers from database"
    )
    write_parser.set_defaults(handler=cmd_write)
    write_parser.add_argument(
        "--db",
        "-d",
        default="NstDatabase.xml",
        help=(
            "path to NES database (download "
            " https://gitlab.com/jgemu/nestopia/-/raw/master/NstDatabase.xml"
            " or https://github.com/0ldsk00l/nestopia/raw/master/NstDatabase.xml "
            " or from http://bootgod.dyndns.org:7777/xml.php"
        ),
    )
    write_parser.add_argument(
        "--dry-run",
        "-n",
        default=False,
        action="store_true",
        help="don't actually change ROMs, just report changes",
    )
    write_parser.add_argument("roms", nargs="+", metavar="rom", help=roms_help)
    args = parser.parse_args()
    # if args.verbose:
    #     logging.getLogger().setLevel(logging.INFO)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
