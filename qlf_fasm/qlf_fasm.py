#!/usr/bin/env python3
"""
QuickLogic qlf-series FPGA FASM to bitstream and bitstream to FASM conversion
utility.
"""
import argparse
import os
import re
import logging
import json

import fasm

from bitstream import TextBitstream

# =============================================================================


class Bit():
    """
    A single bit / segbit. Has an index and a value.
    """

    def __init__(self, index, value):
        self.idx = index
        self.val = value

    @staticmethod
    def from_string(s):
        """
        Parses a string with a bit specification. This should be "[!]<index>".
        For example:
          - "!123"
          - "768"
        """

        s = s.strip()
        if s[0] == "!":
            value = False
            s = s[1:]
        else:
            value = True

        index = int(s)

        return Bit(index, value)

    def __str__(self):
        if self.val:
            return str(self.idx)
        else:
            return "!" + str(self.idx)

    def __repr__(self):
        return str(self)


class Database():
    """
    FASM database representation.
    """

    def __init__(self, root=None):

        # Tiles indexed by grid location
        self.tiles = {}
        # Routing resources indexed by grid location and type
        self.routing = {}

        # Segbits (repeating bit patterns) per block type
        self.segbits = {}
        # Total bitstream length (in bits)
        self.bitstream_size = 0

        # Bit regions
        self.regions = {}

        # Load the database
        if root is not None:
            self.load(root)

    def load(self, path):
        """
        Loads the database given its root directory
        """

        logging.info("Loading FASM database from '{}'".format(path))

        # Load the device file
        device_file = os.path.join(path, "device.json")
        logging.info(" " + device_file)
        with open(device_file, "r") as fp:
            json_root = json.load(fp)

        # Get the basic info
        configuration = json_root["configuration"]
        assert configuration["type"] == "scan_chain", configuration["type"]

        self.bitstream_size = int(configuration["length"])
        self.regions = {
            int(region["id"]): region for region in configuration["regions"]
        }

        # Sort tiles by their locations, load segbits
        for data in json_root["tiles"]:
            loc = (data["x"], data["y"])

            keys = ["type", "region", "offset"]
            tile = {k:v for k, v in data.items() if k in keys}

            assert loc not in self.tiles, (loc, self.tiles[loc], tile)
            self.tiles[loc] = tile

            segbits_name = tile["type"]
            if segbits_name not in self.segbits:

                segbits_file = os.path.join(
                    path, "segbits_{}.db".format(segbits_name)
                )
                self.segbits[segbits_name] = self.load_segbits(segbits_file)

        # Sort routing blocks by their locations, load segbits
        for data in json_root["routing"]:
            loc = (data["x"], data["y"])

            keys = ["type", "variant", "region", "offset"]
            sbox = {k:v for k, v in data.items() if k in keys}

            if loc not in self.routing:
                self.routing[loc] = {}

            assert sbox["type"] not in self.routing[loc], (loc, sbox["type"])
            self.routing[loc][sbox["type"]] = sbox

            segbits_name = "{}_{}".format(sbox["type"], sbox["variant"])
            if segbits_name not in self.segbits:

                segbits_file = os.path.join(
                    path, "segbits_{}.db".format(segbits_name)
                )
                self.segbits[segbits_name] = self.load_segbits(segbits_file)

    @staticmethod
    def load_segbits(file_name):
        """
        Loads segbits. Returns a dict indexed by FASM feature names containing
        segbit sets.
        """
        segbits = {}

        # Load the file
        logging.info(" " + file_name)
        with open(file_name, "r") as fp:
            lines = fp.readlines()

        # Parse segbits
        for line in lines:

            line = line.strip().split()
            if not line:
                continue

            assert len(line) >= 2, line

            feature = line[0]
            bits = [Bit.from_string(s) for s in line[1:]]

            assert feature not in segbits, feature
            segbits[feature] = tuple(bits)

        return segbits

# =============================================================================


class QlfFasmAssembler():
    """
    FASM assembler for QuickLogic QLF devices.
    """

    LOC_RE = re.compile(r"(?P<name>.+)_(?P<x>[0-9]+)__(?P<y>[0-9]+)_$")

    class LookupError(Exception):
        """
        FASM database lookup error exception
        """
        pass

    class FeatureConflict(Exception):
        """
        FASM feature conflict exception
        """
        pass

    def __init__(self, database):
        self.bitstream = bytearray(database.bitstream_size)
        self.database = database

        self.features_by_bits = {}

    def process_fasm_line(self, line):
        """
        Assembles and updates a part of the bistream described by the given
        single FASM line object.
        """

        set_feature = line.set_feature
        if not set_feature:
            return

        # Ignore features that are not set
        if set_feature.value == 0:
            return

        # Split the feature name into parts, check the first part
        parts = set_feature.feature.split(".")
        if len(parts) < 3 or parts[0] != "fpga_top":
            raise self.LookupError

        # Get grid location
        match = self.LOC_RE.fullmatch(parts[1])
        if match is None:
            raise self.LookupError

        loc = (int(match.group("x")), int(match.group("y")))
        name = match.group("name")

        # This feature refers to a block (tile)
        if name.startswith("grid_"):
            name = name.replace("grid_", "")

            # Check
            if loc not in self.database.tiles:
                 raise self.LookupError
            if name not in self.database.segbits:
                 raise self.LookupError

            # Get segbits and offset
            tile = self.database.tiles[loc]
            segbits = self.database.segbits[name]
            region = tile["region"]
            offset = tile["offset"]

        # This feature refers to a routing interconnect
        else:
            name = name.split("_", maxsplit=1)[0]

            if loc not in self.database.routing:
                 raise self.LookupError
            if name not in self.database.routing[loc]:
                 raise self.LookupError

            # Get the routing resource variant
            sbox = self.database.routing[loc][name]
            segbits_name = "{}_{}".format(name, sbox["variant"])

            if segbits_name not in self.database.segbits:
                 raise self.LookupError

            # Get segbits and offset
            segbits = self.database.segbits[segbits_name]
            region = sbox["region"]
            offset = sbox["offset"]

        # Add region offset
        offset += self.database.regions[region]["offset"]

        # Canonicalize - split to single-bit features and process them
        # individually
        for one_feature in fasm.canonical_features(set_feature):

            # Skip cleared features
            assert one_feature.value in [0, 1], one_feature
            if one_feature.value == 0:
                continue

            base_name = one_feature.feature.split(".")
            base_name = ".".join(base_name[2:])

            # Lookup segbits, For 1-bit features try without the index suffix
            # first.
            bits = None
            if one_feature.start in [0, None]:
                key = base_name
                bits = segbits.get(key, None)

            # Try with index
            if bits is None:
                idx = 0 if one_feature.start is None else one_feature.start
                key = "{}[{}]".format(base_name, idx)
                bits = segbits.get(key, None)

            if bits is None:
                logging.debug(one_feature)
                logging.debug(base_name)
                raise self.LookupError

            if not len(bits):
                logging.error(
                    "ERROR: The feature '{}' didn't set/clear any bits!".format(
                    one_feature.feature
                ))

            # Apply them to the bitstream
            for bit in bits:
                address = bit.idx + offset

                # Check for conflict
                if address in self.features_by_bits:
                    if key in self.features_by_bits[address] and \
                       bit.val != self.bitstream[address]:

                        new_bit_act = "set" if bit.val else "clear"
                        org_bit_act = "set" if self.bitstream[address] else "cleared"

                        # Format the error message
                        msg = "The line '{}' wants to {} bit {} already {} by the line '{}'".format(
                            set_feature.feature,
                            new_bit_act,
                            bit.id,
                            org_bit_act,
                            key
                        )
                        raise self.FeatureConflict(msg)
                else:
                    self.features_by_bits[address] = set()

                # Set/clear the bit
                self.bitstream[address] = bit.val
                self.features_by_bits[address].add(key)


    def assemble_bitstream(self, fasm_lines):
        """
        Assembles the bitstream using an interable of FASM line objects
        """
        unknown_features = []

        # Process FASM lines
        for line in fasm_lines:

            try:
                self.process_fasm_line(line)
            except self.LookupError:
                unknown_features.append(line)
                continue
            except self.FeatureConflict:
                raise
            except:
                raise

        return unknown_features

# =============================================================================


class QlfFasmDisassembler():
    """
    FASM disassembler for QuickLogic QLF devices.
    """

    def __init__(self, database):
        self.bitstream = None
        self.database = database

    def match_segbits(self, segbits, offset):
        """
        Matches a segbit pattern at the given offset against the bitstream.
        """
        match = True
        for segbit in segbits:
            address = segbit.idx + offset
            assert address < len(self.bitstream)

            if self.bitstream[address] != segbit.val:
                match = False
                break

        return match

    def disassemble_bitstream(self, bitstream, emit_unset=False):
        """
        Disassembles a bistream.
        """
        features = []

        def emit_feature(feature):
            if emit_unset:
                features.append(full_name + "=1'b{}".format(int(value)))
            else:
                features.append(full_name)

        # Check size
        assert len(bitstream) == self.database.bitstream_size
        self.bitstream = bitstream    

        # Disassemble tiles
        for loc, tile in self.database.tiles.items():

            # Get segbits
            segbits_name = tile["type"]
            assert segbits_name in self.database.segbits
            segbits = self.database.segbits[segbits_name]

            # Format feature prefix
            prefix = "fpga_top.grid_{}_{}__{}_".format(
                tile["type"],
                loc[0],
                loc[1]
            )

            # Check each pattern
            region = tile["region"]
            offset = tile["offset"]

            offset += self.database.regions[region]["offset"]

            for feature, bits in segbits.items():

                # Match
                value = self.match_segbits(bits, offset)
                if not value and not emit_unset:
                    continue

                # Emit
                full_name = prefix + "." + feature
                emit_feature(full_name)

        # Disassemble routing
        for loc, routing in self.database.routing.items():
            for sbox_type, sbox in routing.items():

                # Get segbits
                segbits_name = "{}_{}".format(sbox["type"], sbox["variant"])
                assert segbits_name in self.database.segbits
                segbits = self.database.segbits[segbits_name]

                # Format feature prefix
                prefix = "fpga_top.{}_{}__{}_".format(
                    sbox["type"],
                    loc[0],
                    loc[1]
                )

                # Check each pattern
                region = sbox["region"]
                offset = sbox["offset"]

                offset += self.database.regions[region]["offset"]

                for feature, bits in segbits.items():

                    # Match
                    value = self.match_segbits(bits, offset)
                    if not value and not emit_unset:
                        continue

                    # Emit
                    full_name = prefix + "." + feature
                    emit_feature(full_name)

        return features

# =============================================================================


def fasm_to_bitstream(args, database):
    """
    Implements FASM to bitstream flow
    """

    logging.info("Assembling bitstream from FASM...")

    # Load and parse FASM
    fasm_lines = fasm.parse_fasm_filename(args.i)

    # Assemble
    assembler = QlfFasmAssembler(database)
    unknown_features = assembler.assemble_bitstream(fasm_lines)

    # Got unknown features
    if unknown_features:
        logging.critical("ERROR: Unknown FASM features encountered ({}):".format(
            len(unknown_features)
        ))
        for feature in unknown_features:
            logging.critical(" " + feature.set_feature.feature)
        exit(-1)

    # Compute the expected total length with padding bits
    max_region = max([region["length"] for region in database.regions.values()])
    padded_length = max_region * len(database.regions)

    # Pad the bitstream - add trailing zeros to each chain (region) that is
    # shorter than the longest one.
    padded_bitstream = bytearray(padded_length)
    for region_id, region in database.regions.items():

        dst_address = region_id * max_region
        src_address = region["offset"]
        length = region["length"]

        padded_bitstream[dst_address:dst_address+length] = \
            assembler.bitstream[src_address:src_address+length]

    # Write the bitstream
    logging.info("Writing bitstream...")
    bitstream = TextBitstream(padded_bitstream)
    bitstream.to_file(args.o)


def bitstream_to_fasm(args, database):
    """
    Implements bitstream to FASM flow
    """

    # Load the binary bitstream
    logging.info("Reading bitstream...")
    bitstream = TextBitstream.from_file(args.i)

    # Compute the expected total length with padding bits
    max_region = max([region["length"] for region in database.regions.values()])
    padded_length = max_region * len(database.regions)

    # Verify length
    if len(bitstream.bits) < padded_length:
        logging.error("ERROR: The bistream is too short ({} / {})".format(
            len(bitstream.bits),
            padded_length
        ))
        # TODO: pad

    if len(bitstream.bits) > padded_length:
        logging.warning("WARNING: {} extra trailing bits found ({} / {})".format(
            len(bitstream.bits) - padded_length,
            len(bitstream.bits),
            padded_length
        ))
        # TODO: trim

    # Remove padding bits
    unpadded_bitstream = bytearray(database.bitstream_size)
    for region_id, region in database.regions.items():

        src_address = region_id * max_region
        dst_address = region["offset"]
        length = region["length"]

        unpadded_bitstream[dst_address:dst_address+length] = \
            bitstream.bits[src_address:src_address+length]

    # Disassemble
    logging.info("Disassembling bitstream...")
    disassembler = QlfFasmDisassembler(database)
    features = disassembler.disassemble_bitstream(
        unpadded_bitstream,
        args.unset_features
    )

    # Write FASM file
    logging.info("Writing FASM file...")
    with open(args.o, "w") as fp:
        for feature in features:
            fp.write(feature + "\n")

# =============================================================================


def main():

    # Parse arguments
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        "i",
        type=str,
        help="Input file (FASM or bitstream)"
    )
    parser.add_argument(
        "o",
        type=str,
        help="Output file (FASM or bitstream)"
    )
    parser.add_argument(
        "--db-root",
        type=str,
        default="database",
        help="FASM database root path"
    )
    parser.add_argument(
        "--unset-features",
        action="store_true",
        help="When disassembling write cleared FASM features as well"
    )
    parser.add_argument(
        "--log-level",
        type=str,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="WARNING",
        help="Log level (def. \"WARNING\")"
    )

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, args.log_level.upper()),
    )

    # Check what to do
    inp_ext = os.path.splitext(args.i)[1].lower()
    out_ext = os.path.splitext(args.o)[1].lower()

    if inp_ext == ".fasm" and out_ext in [".bit", ".bin"]:
        action = "fasm2bit"

    elif out_ext == ".fasm" and inp_ext in [".bit", ".bin"]:
        action = "bit2fasm"

    else:
        logging.critical("No known conversion between '{}' and '{}'".format(
            inp_ext,
            out_ext
        ))
        exit(-1)

    # Load the database
    database = Database(args.db_root)

    if action == "fasm2bit":
        fasm_to_bitstream(args, database)

    elif action == "bit2fasm":
        bitstream_to_fasm(args, database)

    else:
        assert False, action

# =============================================================================


if __name__ == "__main__":
    main()