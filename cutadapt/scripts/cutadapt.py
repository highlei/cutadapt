#!/usr/bin/env python
# -*- coding: utf-8 -*-
# kate: word-wrap off; remove-trailing-spaces all;
#
# Copyright (c) 2010-2017 Marcel Martin <marcel.martin@scilifelab.se>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

"""
cutadapt version {version}
Copyright (C) 2010-2017 Marcel Martin <marcel.martin@scilifelab.se>

cutadapt removes adapter sequences from high-throughput sequencing reads.

Usage:
    cutadapt -a ADAPTER [options] [-o output.fastq] input.fastq

For paired-end reads:
    cutadapt -a ADAPT1 -A ADAPT2 [options] -o out1.fastq -p out2.fastq in1.fastq in2.fastq

Replace "ADAPTER" with the actual sequence of your 3' adapter. IUPAC wildcard
characters are supported. The reverse complement is *not* automatically
searched. All reads from input.fastq will be written to output.fastq with the
adapter sequence removed. Adapter matching is error-tolerant. Multiple adapter
sequences can be given (use further -a options), but only the best-matching
adapter will be removed.

Input may also be in FASTA format. Compressed input and output is supported and
auto-detected from the file name (.gz, .xz, .bz2). Use the file name '-' for
standard input/output. Without the -o option, output is sent to standard output.

Citation:

Marcel Martin. Cutadapt removes adapter sequences from high-throughput
sequencing reads. EMBnet.Journal, 17(1):10-12, May 2011.
http://dx.doi.org/10.14806/ej.17.1.200

Use "cutadapt --help" to see all command-line options.
See http://cutadapt.readthedocs.io/ for full documentation.
"""

from __future__ import print_function, division, absolute_import

# Print a helpful error message if the extension modules cannot be imported.
from cutadapt import check_importability
check_importability()

import sys
import time
import errno
from argparse import ArgumentParser, SUPPRESS, HelpFormatter
import functools
import logging
import platform
import textwrap
from xopen import xopen

from cutadapt import seqio, __version__
from cutadapt.adapters import AdapterParser
from cutadapt.modifiers import (LengthTagModifier, SuffixRemover, PrefixSuffixAdder,
	DoubleEncoder, ZeroCapper, PrimerTrimmer, QualityTrimmer, UnconditionalCutter,
	NEndTrimmer, AdapterCutter, NextseqQualityTrimmer, Shortener)
from cutadapt.filters import (NoFilter, PairedNoFilter, Redirector, PairedRedirector,
	LegacyPairedRedirector, TooShortReadFilter, TooLongReadFilter,
	Demultiplexer, NContentFilter, DiscardUntrimmedFilter, DiscardTrimmedFilter)
from cutadapt.report import Statistics, print_report, redirect_standard_output


logger = logging.getLogger()


class CommandlineError(Exception):
	pass


class RestFileWriter(object):
	def __init__(self, file):
		self.file = file

	def write(self, match):
		rest = match.rest()
		if len(rest) > 0:
			print(rest, match.read.name, file=self.file)


class Pipeline(object):
	"""
	Processing pipeline that loops over reads and applies modifiers and filters
	"""
	def __init__(self):
		self._close_files = []

	def register_file_to_close(self, file):
		if file is not None and file is not sys.stdin and file is not sys.stdout:
			self._close_files.append(file)

	def close_files(self):
		for f in self._close_files:
			f.close()

	def process_reads(self):
		raise NotImplementedError()

	def run(self):
		start_time = time.clock()
		(n, total1_bp, total2_bp) = self.process_reads()
		self.close_files()
		elapsed_time = time.clock() - start_time
		# TODO
		m = self.modifiers if hasattr(self, 'modifiers') else self.modifiers1
		m2 = getattr(self, 'modifiers2', [])
		stats = Statistics()
		stats.collect(n, total1_bp, total2_bp, elapsed_time, m, m2, self.filters)
		return stats


class SingleEndPipeline(Pipeline):
	"""
	Processing pipeline for single-end reads
	"""
	def __init__(self, reader, modifiers, filters):
		super(SingleEndPipeline, self).__init__()
		self.reader = reader
		self.modifiers = modifiers
		self.filters = filters

	def process_reads(self):
		"""Run the pipeline. Return statistics"""
		n = 0  # no. of processed reads  # TODO turn into attribute
		total_bp = 0
		for read in self.reader:
			n += 1
			total_bp += len(read.sequence)
			for modifier in self.modifiers:
				read = modifier(read)
			for filter in self.filters:
				if filter(read):
					break
		return (n, total_bp, None)


class PairedEndPipeline(Pipeline):
	"""
	Processing pipeline for paired-end reads.
	"""
	def __init__(self, paired_reader, modifiers1, modifiers2, filters):
		super(PairedEndPipeline, self).__init__()
		self.paired_reader = paired_reader
		self.modifiers1 = modifiers1
		self.modifiers2 = modifiers2
		self.filters = filters

	def process_reads(self):
		n = 0  # no. of processed reads
		total1_bp = 0
		total2_bp = 0
		for read1, read2 in self.paired_reader:
			n += 1
			total1_bp += len(read1.sequence)
			total2_bp += len(read2.sequence)
			for modifier in self.modifiers1:
				read1 = modifier(read1)
			for modifier in self.modifiers2:
				read2 = modifier(read2)
			for filter in self.filters:
				# Stop writing as soon as one of the filters was successful.
				if filter(read1, read2):
					break
		return (n, total1_bp, total2_bp)


def setup_logging(stdout=False, quiet=False):
	"""
	Attach handler to the global logger object
	"""
	# Due to backwards compatibility, logging output is sent to standard output
	# instead of standard error if the -o option is used.
	stream_handler = logging.StreamHandler(sys.stdout if stdout else sys.stderr)
	stream_handler.setFormatter(logging.Formatter('%(message)s'))
	stream_handler.setLevel(logging.ERROR if quiet else logging.INFO)
	logger.setLevel(logging.INFO)
	logger.addHandler(stream_handler)


class CustomUsageHelpFormatter(HelpFormatter):
	def add_usage(self, usage, actions, groups, prefix=None):
		if usage is not SUPPRESS:
			args = usage, actions, groups, ''
			self._add_item(self._format_usage, args)


def get_argument_parser():
	parser = ArgumentParser(usage=__doc__.replace('{version}', __version__),
		formatter_class=CustomUsageHelpFormatter, add_help=False)

	group = parser.add_argument_group('Options')
	group.add_argument('--version', action='version', version=__version__)
	group.add_argument('--help', '-h', action='help',
		help='show this help message and exit')
	group.add_argument("--debug", action='store_true', default=False,
		help="Print debugging information.")
	group.add_argument("-f", "--format",
		help="Input file format; can be either 'fasta', 'fastq' or 'sra-fastq'. "
			"Ignored when reading csfasta/qual files. Default: auto-detect "
			"from file name extension.")

	# Hidden option for now
	parser.add_argument("--gc-content", type=float, default=50,  # it's a percentage
		help=SUPPRESS)

	group = parser.add_argument_group("Finding adapters",
		description="Parameters -a, -g, -b specify adapters to be removed from "
			"each read (or from the first read in a pair if data is paired). "
			"If specified multiple times, only the best matching adapter is "
			"trimmed (but see the --times option). When the special notation "
			"'file:FILE' is used, adapter sequences are read from the given "
			"FASTA file.")
	group.add_argument("-a", "--adapter", action="append", default=[], metavar="ADAPTER",
		dest="adapters",
		help="Sequence of an adapter ligated to the 3' end (paired data: of the "
			"first read). The adapter and subsequent bases are trimmed. If a "
			"'$' character is appended ('anchoring'), the adapter is only "
			"found if it is a suffix of the read.")
	group.add_argument("-g", "--front", action="append", default=[], metavar="ADAPTER",
		help="Sequence of an adapter ligated to the 5' end (paired data: of the "
			"first read). The adapter and any preceding bases are trimmed. "
			"Partial matches at the 5' end are allowed. If a '^' character is "
			"prepended ('anchoring'), the adapter is only found if it is a "
			"prefix of the read.")
	group.add_argument("-b", "--anywhere", action="append", default=[], metavar="ADAPTER",
		help="Sequence of an adapter that may be ligated to the 5' or 3' end "
			"(paired data: of the first read). Both types of matches as "
			"described under -a und -g are allowed. If the first base of the "
			"read is part of the match, the behavior is as with -g, otherwise "
			"as with -a. This option is mostly for rescuing failed library "
			"preparations - do not use if you know which end your adapter was "
			"ligated to!")
	group.add_argument("-e", "--error-rate", type=float, default=0.1,
		help="Maximum allowed error rate (no. of errors divided by the length "
			"of the matching region). Default: %(default)s")
	group.add_argument("--no-indels", action='store_false', dest='indels', default=True,
		help="Allow only mismatches in alignments. "
			"Default: allow both mismatches and indels")
	group.add_argument("-n", "--times", type=int, metavar="COUNT", default=1,
		help="Remove up to COUNT adapters from each read. Default: %(default)s")
	group.add_argument("-O", "--overlap", type=int, metavar="MINLENGTH", default=3,
		help="If the overlap between the read and the adapter is shorter than "
			"MINLENGTH, the read is not modified. Reduces the no. of bases "
			"trimmed due to random adapter matches. Default: %(default)s")
	group.add_argument("--match-read-wildcards", action="store_true", default=False,
		help="Interpret IUPAC wildcards in reads. Default: %(default)s")
	group.add_argument("-N", "--no-match-adapter-wildcards", action="store_false",
		default=True, dest='match_adapter_wildcards',
		help="Do not interpret IUPAC wildcards in adapters.")
	group.add_argument("--no-trim", dest='action', action='store_const', const=None,
		help="Match and redirect reads to output/untrimmed-output as usual, "
			"but do not remove adapters.")
	group.add_argument("--mask-adapter", dest='action', action='store_const', const='mask',
		help="Mask adapters with 'N' characters instead of trimming them.")

	group = parser.add_argument_group("Additional read modifications")
	group.add_argument("-u", "--cut", action='append', default=[], type=int, metavar="LENGTH",
		help="Remove bases from each read (first read only if paired). "
			"If LENGTH is positive, remove bases from the beginning. "
			"If LENGTH is negative, remove bases from the end. "
			"Can be used twice if LENGTHs have different signs.")
	group.add_argument("--nextseq-trim", type=int, default=None, metavar="3'CUTOFF",
		help="NextSeq-specific quality trimming (each read). Trims also dark "
			"cycles appearing as high-quality G bases (EXPERIMENTAL).")
	group.add_argument("-q", "--quality-cutoff", default=None, metavar="[5'CUTOFF,]3'CUTOFF",
		help="Trim low-quality bases from 5' and/or 3' ends of each read before "
			"adapter removal. Applied to both reads if data is paired. If one "
			"value is given, only the 3' end is trimmed. If two "
			"comma-separated cutoffs are given, the 5' end is trimmed with "
			"the first cutoff, the 3' end with the second.")
	group.add_argument("--quality-base", type=int, default=33,
		help="Assume that quality values in FASTQ are encoded as ascii(quality "
			"+ QUALITY_BASE). This needs to be set to 64 for some old Illumina "
			"FASTQ files. Default: %(default)s")
	group.add_argument("--length", "-l", type=int, default=None, metavar="LENGTH",
			help="Shorten reads to LENGTH. This and the following modifications"
			"are applied after adapter trimming.")
	group.add_argument("--trim-n", action='store_true', default=False,
		help="Trim N's on ends of reads.")
	group.add_argument("--length-tag", metavar="TAG",
		help="Search for TAG followed by a decimal number in the description "
			"field of the read. Replace the decimal number with the correct "
			"length of the trimmed read. For example, use --length-tag 'length=' "
			"to correct fields like 'length=123'.")
	group.add_argument("--strip-suffix", action='append', default=[],
		help="Remove this suffix from read names if present. Can be given multiple times.")
	group.add_argument("-x", "--prefix", default='',
		help="Add this prefix to read names. Use {name} to insert the name of the matching adapter.")
	group.add_argument("-y", "--suffix", default='',
		help="Add this suffix to read names; can also include {name}")

	group = parser.add_argument_group("Filtering of processed reads")
	group.add_argument("-m", "--minimum-length", type=int, default=0, metavar="LENGTH",
		help="Discard trimmed reads that are shorter than LENGTH. Reads that "
			"are too short even before adapter removal are also discarded. In "
			"colorspace, an initial primer is not counted. Default: 0")
	group.add_argument("-M", "--maximum-length", type=int, default=sys.maxsize, metavar="LENGTH",
		help="Discard trimmed reads that are longer than LENGTH. "
			"Reads that are too long even before adapter removal "
			"are also discarded. In colorspace, an initial primer "
			"is not counted. Default: no limit")
	group.add_argument("--max-n", type=float, default=-1.0, metavar="COUNT",
		help="Discard reads with too many N bases. If COUNT is an integer, it "
			"is treated as the absolute number of N bases. If it is between 0 "
			"and 1, it is treated as the proportion of N's allowed in a read.")
	group.add_argument("--discard-trimmed", "--discard", action='store_true', default=False,
		help="Discard reads that contain an adapter. Also use -O to avoid "
			"discarding too many randomly matching reads!")
	group.add_argument("--discard-untrimmed", "--trimmed-only", action='store_true', default=False,
		help="Discard reads that do not contain the adapter.")

	group = parser.add_argument_group("Output")
	group.add_argument("--quiet", default=False, action='store_true',
		help="Print only error messages.")
	group.add_argument("-o", "--output", metavar="FILE",
		help="Write trimmed reads to FILE. FASTQ or FASTA format is chosen "
			"depending on input. The summary report is sent to standard output. "
			"Use '{name}' in FILE to demultiplex reads into multiple "
			"files. Default: write to standard output")
	group.add_argument("--info-file", metavar="FILE",
		help="Write information about each read and its adapter matches into FILE. "
			"See the documentation for the file format.")
	group.add_argument("-r", "--rest-file", metavar="FILE",
		help="When the adapter matches in the middle of a read, write the "
			"rest (after the adapter) into FILE.")
	group.add_argument("--wildcard-file", metavar="FILE",
		help="When the adapter has N bases (wildcards), write adapter bases "
			"matching wildcard positions to FILE. When there are indels in the "
			"alignment, this will often not be accurate.")
	group.add_argument("--too-short-output", metavar="FILE",
		help="Write reads that are too short (according to length specified by "
		"-m) to FILE. Default: discard reads")
	group.add_argument("--too-long-output", metavar="FILE",
		help="Write reads that are too long (according to length specified by "
		"-M) to FILE. Default: discard reads")
	group.add_argument("--untrimmed-output", default=None, metavar="FILE",
		help="Write reads that do not contain any adapter to FILE. Default: "
			"output to same file as trimmed reads")

	group = parser.add_argument_group("Colorspace options")
	group.add_argument("-c", "--colorspace", action='store_true', default=False,
		help="Enable colorspace mode: Also trim the color that is adjacent to the found adapter.")
	group.add_argument("-d", "--double-encode", action='store_true', default=False,
		help="Double-encode colors (map 0,1,2,3,4 to A,C,G,T,N).")
	group.add_argument("-t", "--trim-primer", action='store_true', default=False,
		help="Trim primer base and the first color (which is the transition "
			"to the first nucleotide)")
	group.add_argument("--strip-f3", action='store_true', default=False,
		help="Strip the _F3 suffix of read names")
	group.add_argument("--maq", "--bwa", action='store_true', default=False,
		help="MAQ- and BWA-compatible colorspace output. This enables -c, -d, "
			"-t, --strip-f3 and -y '/1'.")
	group.add_argument("--no-zero-cap", dest='zero_cap', action='store_false',
		help="Do not change negative quality values to zero in colorspace "
			"data. By default, they are since many tools have problems with "
			"negative qualities.")
	group.add_argument("--zero-cap", "-z", action='store_true',
		help="Change negative quality values to zero. This is enabled "
		"by default when -c/--colorspace is also enabled. Use the above option "
		"to disable it.")
	parser.set_defaults(zero_cap=None, action='trim')

	group = parser.add_argument_group("Paired-end options", description="The "
		"-A/-G/-B/-U options work like their -a/-b/-g/-u counterparts, but "
		"are applied to the second read in each pair.")
	group.add_argument("-A", dest='adapters2', action='append', default=[], metavar='ADAPTER',
		help="3' adapter to be removed from second read in a pair.")
	group.add_argument("-G", dest='front2', action='append', default=[], metavar='ADAPTER',
		help="5' adapter to be removed from second read in a pair.")
	group.add_argument("-B", dest='anywhere2', action='append', default=[], metavar='ADAPTER',
		help="5'/3 adapter to be removed from second read in a pair.")
	group.add_argument("-U", dest='cut2', action='append', default=[], type=int, metavar="LENGTH",
		help="Remove LENGTH bases from second read in a pair (see --cut).")
	group.add_argument("-p", "--paired-output", metavar="FILE",
		help="Write second read in a pair to FILE.")
	# Setting the default for pair_filter to None allows us to find out whether
	# the option was used at all.
	group.add_argument("--pair-filter", metavar='(any|both)', default=None,
		choices=("any", "both"),
		help="Which of the reads in a paired-end read have to match the "
			"filtering criterion in order for it to be filtered. "
			"Default: any")
	group.add_argument("--interleaved", action='store_true', default=False,
		help="Read and write interleaved paired-end reads.")
	group.add_argument("--untrimmed-paired-output", metavar="FILE",
		help="Write second read in a pair to this FILE when no adapter "
			"was found in the first read. Use this option together with "
			"--untrimmed-output when trimming paired-end reads. Default: output "
			"to same file as trimmed reads")
	group.add_argument("--too-short-paired-output", metavar="FILE", default=None,
		help="Write second read in a pair to this file if pair is too short. "
			"Use together with --too-short-output.")
	group.add_argument("--too-long-paired-output", metavar="FILE", default=None,
		help="Write second read in a pair to this file if pair is too long. "
			"Use together with --too-long-output.")

	return parser


def pipeline_from_parsed_args(argus, default_outfile):
	"""
	Setup a processing pipeline from parsed command-line args.

	If there are any problems parsing the arguments, a CommandlineError is thrown.
	"""
	if len(args) == 0:
		raise CommandlineError("At least one parameter needed: name of a FASTA or FASTQ file.")
	elif len(args) > 2:
		raise CommandlineError("Too many parameters.")
	input_filename = args[0]
	if input_filename.endswith('.qual'):
		raise CommandlineError("If a .qual file is given, it must be the second argument.")

	# Find out which 'mode' we need to use.
	# Default: single-read trimming (neither -p nor -A/-G/-B/-U/--interleaved given)
	paired = False
	if args.paired_output:
		# Modify first read only, keep second in sync (-p given, but not -A/-G/-B/-U).
		# This exists for backwards compatibility ('legacy mode').
		paired = 'first'

	# Switch off legacy mode if certain options given
	if paired and args.nextseq_trim:
		paired = 'both'
	if (args.adapters2 or args.front2 or args.anywhere2 or
			args.cut2 or args.interleaved or args.pair_filter or
			args.too_short_paired_output or args.too_long_paired_output):
		# Full paired-end trimming when both -p and -A/-G/-B/-U given
		# Read modifications (such as quality trimming) are applied also to second read.
		paired = 'both'

	if paired and len(args) == 1 and not args.interleaved:
		raise CommandlineError("When paired-end trimming is enabled via -A/-G/-B/-U/"
			"--interleaved or -p, two input files are required.")
	if not paired:
		if args.untrimmed_paired_output:
			raise CommandlineError("Option --untrimmed-paired-output can only be used when "
				"trimming paired-end reads (with option -p).")

	interleaved_input = False
	interleaved_output = False
	if args.interleaved:
		interleaved_input = len(args) == 1
		interleaved_output = not args.paired_output
		if not interleaved_input and not interleaved_output:
			raise CommandlineError("When --interleaved is used, you cannot provide both two input files and two output files")

	# Assign input_paired_filename and quality_filename
	input_paired_filename = None
	quality_filename = None
	if paired:
		if not interleaved_input:
			input_paired_filename = args[1]
		if not interleaved_output:
			if not args.paired_output:
				raise CommandlineError("When paired-end trimming is enabled via -A/-G/-B/-U, "
					"a second output file needs to be specified via -p (--paired-output).")
			if not args.output:
				raise CommandlineError("When you use -p or --paired-output, you must also "
					"use the -o option.")

		if bool(args.untrimmed_output) != bool(args.untrimmed_paired_output):
			raise CommandlineError("When trimming paired-end reads, you must use either none "
				"or both of the --untrimmed-output/--untrimmed-paired-output args.")
		if args.too_short_output and not args.too_short_paired_output:
			raise CommandlineError("When using --too-short-output with paired-end "
				"reads, you also need to use --too-short-paired-output")
		if args.too_long_output and not args.too_long_paired_output:
			raise CommandlineError("When using --too-long-output with paired-end "
				"reads, you also need to use --too-long-paired-output")
	elif len(args) == 2:
		quality_filename = args[1]
		if args.format is not None:
			raise CommandlineError("If a pair of .fasta and .qual files is given, the -f/--format parameter cannot be used.")

	if args.format is not None and args.format.lower() not in ['fasta', 'fastq', 'sra-fastq']:
		raise CommandlineError("The input file format must be either 'fasta', 'fastq' or "
			"'sra-fastq' (not '{0}').".format(args.format))

	# Open input file(s)
	try:
		reader = seqio.open(input_filename, file2=input_paired_filename,
				qualfile=quality_filename, colorspace=args.colorspace,
				fileformat=args.format, interleaved=interleaved_input)
	except (seqio.UnknownFileType, IOError) as e:
		raise CommandlineError(e)

	if args.quality_cutoff is not None:
		cutoffs = args.quality_cutoff.split(',')
		if len(cutoffs) == 1:
			try:
				cutoffs = [0, int(cutoffs[0])]
			except ValueError as e:
				raise CommandlineError("Quality cutoff value not recognized: {0}".format(e))
		elif len(cutoffs) == 2:
			try:
				cutoffs = [int(cutoffs[0]), int(cutoffs[1])]
			except ValueError as e:
				raise CommandlineError("Quality cutoff value not recognized: {0}".format(e))
		else:
			raise CommandlineError("Expected one value or two values separated by comma for the quality cutoff")
	else:
		cutoffs = None

	open_writer = functools.partial(seqio.open, mode='w',
		qualities=reader.delivers_qualities, colorspace=args.colorspace)

	if args.pair_filter is None:
		args.pair_filter = 'any'
	min_affected = 2 if args.pair_filter == 'both' else 1
	if not paired:
		filter_wrapper = Redirector
	elif paired == 'first':
		filter_wrapper = LegacyPairedRedirector
	elif paired == 'both':
		filter_wrapper = functools.partial(PairedRedirector, min_affected=min_affected)
	filters = []
	# TODO open_files = []
	too_short_writer = None  # too short reads go here
	# TODO pass file name to TooShortReadFilter, add a .close() method?
	if args.minimum_length > 0:
		if args.too_short_output:
			too_short_writer = open_writer(args.too_short_output, args.too_short_paired_output)
		filters.append(filter_wrapper(too_short_writer, TooShortReadFilter(args.minimum_length)))
	too_long_writer = None  # too long reads go here
	if args.maximum_length < sys.maxsize:
		if args.too_long_output is not None:
			too_long_writer = open_writer(args.too_long_output, args.too_long_paired_output)
		filters.append(filter_wrapper(too_long_writer, TooLongReadFilter(args.maximum_length)))

	if args.max_n != -1:
		filters.append(filter_wrapper(None, NContentFilter(args.max_n)))

	if int(args.discard_trimmed) + int(args.discard_untrimmed) + int(args.untrimmed_output is not None) > 1:
		raise CommandlineError("Only one of the --discard-trimmed, --discard-untrimmed "
			"and --untrimmed-output options can be used at the same time.")
	demultiplexer = None
	untrimmed_writer = None
	writer = None
	if args.output is not None and '{name}' in args.output:
		if args.discard_trimmed:
			raise CommandlineError("Do not use --discard-trimmed when demultiplexing.")
		if paired:
			raise CommandlineError("Demultiplexing not supported for paired-end files, yet.")
		untrimmed = args.output.replace('{name}', 'unknown')
		if args.untrimmed_output:
			untrimmed = args.untrimmed_output
		if args.discard_untrimmed:
			untrimmed = None
		demultiplexer = Demultiplexer(args.output, untrimmed,
			qualities=reader.delivers_qualities, colorspace=args.colorspace)
		filters.append(demultiplexer)
	else:
		# Set up the remaining filters to deal with --discard-trimmed,
		# --discard-untrimmed and --untrimmed-output. These options
		# are mutually exclusive in order to avoid brain damage.
		if args.discard_trimmed:
			filters.append(filter_wrapper(None, DiscardTrimmedFilter()))
		elif args.discard_untrimmed:
			filters.append(filter_wrapper(None, DiscardUntrimmedFilter()))
		elif args.untrimmed_output:
			untrimmed_writer = open_writer(args.untrimmed_output,
				args.untrimmed_paired_output)
			filters.append(filter_wrapper(untrimmed_writer, DiscardUntrimmedFilter()))

		# Finally, figure out where the reads that passed all the previous
		# filters should go.
		if args.output is not None:
			writer = open_writer(args.output, args.paired_output, interleaved=interleaved_output)
		else:
			writer = open_writer(default_outfile, interleaved=interleaved_output)
		if not paired:
			filters.append(NoFilter(writer))
		else:
			filters.append(PairedNoFilter(writer))

	if args.maq:
		args.colorspace = True
		args.double_encode = True
		args.trim_primer = True
		args.strip_suffix.append('_F3')
		args.suffix = "/1"
	if args.zero_cap is None:
		args.zero_cap = args.colorspace
	if args.trim_primer and not args.colorspace:
		raise CommandlineError("Trimming the primer makes only sense in colorspace.")
	if args.double_encode and not args.colorspace:
		raise CommandlineError("Double-encoding makes only sense in colorspace.")
	if args.anywhere and args.colorspace:
		raise CommandlineError("Using --anywhere with colorspace reads is currently not supported "
			"(if you think this may be useful, contact the author).")
	if not (0 <= args.error_rate <= 1.):
		raise CommandlineError("The maximum error rate must be between 0 and 1.")
	if args.overlap < 1:
		raise CommandlineError("The overlap must be at least 1.")

	if args.rest_file is not None:
		args.rest_file = xopen(args.rest_file, 'w')
		rest_writer = RestFileWriter(args.rest_file)
	else:
		rest_writer = None
	if args.info_file is not None:
		args.info_file = xopen(args.info_file, 'w')
	if args.wildcard_file is not None:
		args.wildcard_file = xopen(args.wildcard_file, 'w')

	if args.colorspace:
		if args.match_read_wildcards:
			raise CommandlineError('IUPAC wildcards not supported in colorspace')
		args.match_adapter_wildcards = False

	adapter_parser = AdapterParser(
		colorspace=args.colorspace,
		max_error_rate=args.error_rate,
		min_overlap=args.overlap,
		read_wildcards=args.match_read_wildcards,
		adapter_wildcards=args.match_adapter_wildcards,
		indels=args.indels)

	try:
		adapters = adapter_parser.parse_multi(args.adapters, args.anywhere, args.front)
		adapters2 = adapter_parser.parse_multi(args.adapters2, args.anywhere2, args.front2)
	except IOError as e:
		if e.errno == errno.ENOENT:
			raise CommandlineError(e)
		raise
	except ValueError as e:
		raise CommandlineError(e)
	if args.debug:
		for adapter in adapters + adapters2:
			adapter.enable_debug()

	# Create the single-end processing pipeline (a list of "modifiers")
	modifiers = []
	if args.cut:
		if len(args.cut) > 2:
			raise CommandlineError("You cannot remove bases from more than two ends.")
		if len(args.cut) == 2 and args.cut[0] * args.cut[1] > 0:
			raise CommandlineError("You cannot remove bases from the same end twice.")
		for cut in args.cut:
			if cut != 0:
				modifiers.append(UnconditionalCutter(cut))

	if args.nextseq_trim is not None:
		modifiers.append(NextseqQualityTrimmer(args.nextseq_trim, args.quality_base))
	if cutoffs:
		modifiers.append(QualityTrimmer(cutoffs[0], cutoffs[1], args.quality_base))
	if adapters:
		adapter_cutter = AdapterCutter(adapters, args.times,
				args.wildcard_file, args.info_file,
				rest_writer, args.action)
		modifiers.append(adapter_cutter)

	# Modifiers that apply to both reads of paired-end reads unless in legacy mode
	modifiers_both = []
	if args.length is not None:
		modifiers_both.append(Shortener(args.length))
	if args.trim_n:
		modifiers_both.append(NEndTrimmer())
	if args.length_tag:
		modifiers_both.append(LengthTagModifier(args.length_tag))
	if args.strip_f3:
		args.strip_suffix.append('_F3')
	for suffix in args.strip_suffix:
		modifiers_both.append(SuffixRemover(suffix))
	if args.prefix or args.suffix:
		modifiers_both.append(PrefixSuffixAdder(args.prefix, args.suffix))
	if args.double_encode:
		modifiers_both.append(DoubleEncoder())
	if args.zero_cap and reader.delivers_qualities:
		modifiers_both.append(ZeroCapper(quality_base=args.quality_base))
	if args.trim_primer:
		modifiers_both.append(PrimerTrimmer)
	modifiers.extend(modifiers_both)

	# For paired-end data, create a second processing pipeline.
	# However, if no second-read adapters were given (via -A/-G/-B/-U), we need to
	# be backwards compatible and *no modifications* are done to the second read.
	modifiers2 = []
	if paired == 'both':
		if args.cut2:
			if len(args.cut2) > 2:
				raise CommandlineError("You cannot remove bases from more than two ends.")
			if len(args.cut2) == 2 and args.cut2[0] * args.cut2[1] > 0:
				raise CommandlineError("You cannot remove bases from the same end twice.")
			for cut in args.cut2:
				if cut != 0:
					modifiers2.append(UnconditionalCutter(cut))

		if args.nextseq_trim is not None:
			modifiers2.append(NextseqQualityTrimmer(args.nextseq_trim, args.quality_base))
		if cutoffs:
			modifiers2.append(QualityTrimmer(cutoffs[0], cutoffs[1], args.quality_base))
		if adapters2:
			adapter_cutter2 = AdapterCutter(adapters2, args.times,
					None, None, None, args.action)
			modifiers2.append(adapter_cutter2)
		modifiers2.extend(modifiers_both)

	if paired:
		pipeline = PairedEndPipeline(reader, modifiers, modifiers2, filters)
	else:
		pipeline = SingleEndPipeline(reader, modifiers, filters)

	# TODO the following should be done some other way
	pipeline.paired = paired
	pipeline.error_rate = args.error_rate
	pipeline.n_adapters = len(adapters) + len(adapters2)
	pipeline.should_print_warning = paired == 'first' and (modifiers_both or
			cutoffs or len(filters) > 1)
	for f in [writer, untrimmed_writer,
			args.rest_file, args.wildcard_file,
			args.info_file, too_short_writer, too_long_writer,
			args.info_file, demultiplexer]:
		pipeline.register_file_to_close(f)
	return pipeline


def main(cmdlineargs=None, default_outfile=sys.stdout):
	"""
	Main function that evaluates command-line parameters and iterates
	over all reads.

	default_outfile is the file to which trimmed reads are sent if the ``-o``
	parameter is not used.
	"""
	parser = get_argument_parser()
	if cmdlineargs is None:
		cmdlineargs = sys.argv[1:]
	args = parser.parse_args(args=cmdlineargs)
	# Setup logging only if there are not already any handlers (can happen when
	# this function is being called externally such as from unit tests)
	if not logging.root.handlers:
		setup_logging(stdout=bool(args.output), quiet=args.quiet)

	if not 0 <= args.gc_content <= 100:
		parser.error("GC content must be given as percentage between 0 and 100")
	try:
		pipeline = pipeline_from_parsed_args(args, default_outfile)
	except CommandlineError as e:
		parser.error(e)

	implementation = platform.python_implementation()
	opt = ' (' + implementation + ')' if implementation != 'CPython' else ''
	logger.info("This is cutadapt %s with Python %s%s", __version__,
		platform.python_version(), opt)
	logger.info("Command line parameters: %s", " ".join(cmdlineargs))
	logger.info("Trimming %s adapter%s with at most %.1f%% errors in %s mode ...",
		pipeline.n_adapters, 's' if pipeline.n_adapters != 1 else '',
		pipeline.error_rate * 100,
		{ False: 'single-end', 'first': 'paired-end legacy', 'both': 'paired-end' }[pipeline.paired])

	if pipeline.should_print_warning:
		logger.warning('\n'.join(textwrap.wrap('WARNING: Legacy mode is '
			'enabled. Read modification and filtering options *ignore* '
			'the second read. To switch to regular paired-end mode, '
			'provide the --pair-filter=any option or use any of the '
			'-A/-B/-G/-U/--interleaved options.'
			)))

	try:
		stats = pipeline.run()
	except KeyboardInterrupt as e:
		print("Interrupted", file=sys.stderr)
		sys.exit(130)
	except IOError as e:
		if e.errno == errno.EPIPE:
			sys.exit(1)
		raise
	except (seqio.FormatError, EOFError) as e:
		sys.exit("cutadapt: error: {0}".format(e))

	if not args.quiet:
		# send statistics to stderr if result was sent to stdout
		stat_file = sys.stderr if args.output is None else None
		with redirect_standard_output(stat_file):
			print_report(stats, args.gc_content / 100)


if __name__ == '__main__':
	main()
