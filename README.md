inestool: Read/write iNES readers on NES ROMs
===============================================

This is a little tool I wrote for fun to add headers to a [No-Intro ROM set on archive.org][ROM set].  Most of the NES ROMs there have had their headers stripped.  Some NES emulators, and probably also some flash carts, still need these headers, as they contain information about how the cartridge is wired up and what type of system it is supposed to be played on, information that is not in the ROM itself.

[ROM set]: https://archive.org/details/No-Intro-Collection_2015-03-03

Some software, such as the [Nestopia][] emulator, doesn't usually need these headers because it comes with its own database of NES games that overrides the notoriously incorrect headers found on ROMs in the wild.  This little Python script uses an NES cartridge database, such as the one distributed with Nestopia, to add or correct the headers in NES ROMs.

[Nestopia]: https://github.com/rdanbrook/nestopia

## Usage

Read the iNES header from a ROM:

```
$ python inestool.py read 'Super Mario Bros. (World).nes'
Super Mario Bros. (World).nes (D445F698): no header
```

`D445F698` is the CRC32 for the ROM.  (I could have used SHA-1 but I was lazy and didn't want to deal with printing/formatting around the longer value.)

Now I've inserted all zero bytes for the iNES header:

```
$ python inestool.py read 'Super Mario Bros. (World).nes'
Super Mario Bros. (World).nes (D445F698):
	PRG ROM         : 0 KiB
	PRG RAM         : 0 KiB
	CHR ROM         : CHR RAM
	Mapper          : 0
	Mirroring       : Horizontal
	TV System       : NTSC
	Has NVRAM       : False
	Has Trainer     : False
	Is PlayChoice-10: False
	Is VS. UniSystem: False
```

Let's correct the header:

```
$ python inestool.py write 'Super Mario Bros. (World).nes'
Super Mario Bros. (World).nes (D445F698): header differs from database, will update header
	PRG ROM: expected 32 KiB, read 0 KiB
	CHR ROM: expected 8 KiB, read CHR RAM
	Mirroring: expected Vertical, read Horizontal
$ python inestool.py read 'Super Mario Bros. (World).nes'
Super Mario Bros. (World).nes (D445F698):
	PRG ROM         : 32 KiB
	PRG RAM         : 0 KiB
	CHR ROM         : 8 KiB
	Mapper          : 0
	Mirroring       : Vertical
	TV System       : NTSC
	Has NVRAM       : False
	Has Trainer     : False
	Is PlayChoice-10: False
	Is VS. UniSystem: False
```

That looks right to me.  Feel free to compare with [Super Mario Bros. on bootgod's site][bootgod SMB].

[bootgod SMB]: http://bootgod.dyndns.org:7777/profile.php?id=270


## Limitations

- Doesn't handle UNIF headers.
- Bails on ROMs that have "trainer" data between the iNES header and the ROM itself.
- Doesn't handle NES 2.0 headers.
- Can only read from 7-Zip archives (with pylzma; see below), can't write to them.
- Barely tested.


## Requirements

Requires Python 2.7.  If you want to read 7-Zip archives you'll also need [pylzma][] installed (e.g. `pip install pylzma`).  Only tested on OS X so far, but should work on *nix and even Windows, I expect.

You'll need to download a database to use the `write` sub-command.  I've been using [Nestopia's][Nestopia]: download `NstDatabase.xml` from there.  Alternatively you could try using [bootgod's database][] which should also work.  Check out the `--db` option to `write` to point it at a database if yours isn't named `NstDatabase.xml` in the current directory.

[bootgod's database]: http://bootgod.dyndns.org:7777/xml.php


## Credits

I wrote this mostly for fun, and because [OpenEmu][] wouldn't recognize these ROMs without it.  [Greg Kennedy wrote a Python script to do this first][Kennedy's script].  I started writing my script independent of his, but ended up having to look at his to see how he handled a few things, like mirroring and battery-backed RAM.  Lots of people have used his script to good effect, if forum posts are to be believed.

[Nestopia][] sources were of some help, though I still don't understand half of what I read there.  Still, the thing seems relatively well designed for such a complex and long-lived piece of software.

[bootgod's site][] has been helpful for trying to understand what I should be writing in the headers.

The [Nesdev Wiki][] has been a great reference, especially the page on [iNES headers][].

[OpenEmu]: http://openemu.org/
[Kennedy's script]: http://greg-kennedy.com/wordpress/2012/05/30/ines-header-fixer/
[bootgod's site]: http://bootgod.dyndns.org:7777/home.php
[Nesdev Wiki]: http://wiki.nesdev.com/w/index.php/Nesdev_Wiki
[iNES headers]: http://wiki.nesdev.com/w/index.php/INES
