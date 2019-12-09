"""
Split reads by haplotype

This subcommand reads either a FASTQ or a BAM file and a list of haplotype assignments
(such as generated by whatshap haplotag --output-haplotag-list); it then outputs one
FASTQ or BAM per haplotype.

1. BAM mode is intended for unmapped BAMs (such as provided by PacBio).
2. The output format is the same as the input format (FASTQ or BAM).
  (Reading BAM but writing FASTQ or vice versa is not possible.)

Examples:

    whatshap split --output-h1 h1.fastq.gz --output-h2 h2.fastq.gz reads.fastq.gz haplotypes.txt
    whatshap split --output-h1 h1.bam --output-h2 h2.bam reads.bam haplotypes.txt

Tetraploid:

    whatshap split -o h1.bam -o h2.bam -o h3.bam -o h4.bam reads.bam haplotypes.txt
"""
import logging
import os
import pysam
from collections import defaultdict, Counter
import itertools

from xopen import xopen

from contextlib import ExitStack
from whatshap.utils import detect_file_format
from whatshap.timer import StageTimer

logger = logging.getLogger(__name__)


# fmt: off
def add_arguments(parser):
    arg = parser.add_argument
    arg('--output-h1', metavar='FILE',
        help='Output haplotype 1 reads to FILE (.gz supported)')
    arg('--output-h2', metavar='FILE',
        help='Output haplotype 2 reads to FILE (.gz supported)')
    arg('--output', '-o', dest='outputs', metavar='FILE', action='append',
        help='Output haplotype reads to FILE. Use this option as many times as there are haplotypes in the input.'
        ' The first -o is used for H1, second for H2 etc.')
    arg('--output-untagged',
        help='Output file to write untagged reads to (.gz supported)')
    arg('--add-untagged', default=False, action='store_true',
        help='Add reads without tag to all (H1, H2, H3, H4) output streams.')
    arg('--only-largest-block', default=False, action='store_true',
        help='Only consider reads to be tagged if they belong to the largest '
        'phased block (in terms of read count) on their respective chromosome')
    arg('--discard-unknown-reads', default=False, action='store_true',
        help='Only check the haplotype of reads listed in the haplotag list file. '
        'Reads (read names) not contained in this file will be discarded. '
        'In the default case (= keep unknown reads), those reads would be '
        'considered untagged and end up in the respective output file. '
        'Please be sure that the read names match between the input FASTQ/BAM '
        'and the haplotag list file.')
    arg('--read-lengths-histogram',
        help='Output file to write read lengths histogram to in tab-separated format.')
    arg('reads_file', metavar='READS', help='Input FASTQ/BAM file with reads (FASTQ can be gzipped)')
    arg('list_file', metavar='LIST',
        help='Tab-separated list with (at least) two columns <readname> and <haplotype> (can be gzipped). '
        'Currently, the haplotypes have to be named H1, H2, H3 etc. (or none). Alternatively, the '
        'output of the "haplotag" command can be used (4 columns), and this is required for using '
        'the "--only-largest-block" option (need phaseset and chromosome info).')
# fmt: on


def validate(args, parser):
    if (
        (args.output_h1 is None)
        and (args.output_h2 is None)
        and (not args.outputs)
        and (args.output_untagged is not None)
    ):
        parser.error(
            "Nothing to be done since neither --output-h1/h2, --outputs/-o nor --output-untagged are given."
        )
    if ((args.output_h1 is not None) or (args.output_h2 is not None)) and args.outputs is not None:
        parser.error("--output-h1/-h2 cannot be used together with --outputs/-o")


def select_reads_in_largest_phased_blocks(block_sizes, block_to_readnames):
    selected_reads = set()
    logger.info("Determining largest blocks/phasesets per chromosome")
    for chromosome, block_counts in block_sizes.items():
        block_name, reads_in_block = block_counts.most_common(1)[0]
        logger.info(
            "Chromosome: {} - Phaseset: {} - Tagged reads: {}".format(
                chromosome, block_name, reads_in_block
            )
        )
        selected_reads = selected_reads.union(set(block_to_readnames[(chromosome, block_name)]))
    logger.info(
        "Total number of haplo-tagged reads in all largest phased blocks: {}".format(
            len(selected_reads)
        )
    )
    return selected_reads


def process_haplotag_list_file(
    haplolist, line_parser, only_largest_blocks, discard_unknown_reads, ploidy: int
):
    if not haplolist.readline().startswith("#"):
        haplolist.seek(0)

    # needed to determine largest phased block
    block_sizes = defaultdict(Counter)
    # for later removal of reads not in largest phased block;
    # since this can grow quite a bit, only fill if needed
    blocks_to_readnames = defaultdict(set)

    # this set should not be too large given
    # that the haplotag list file contains only
    # a subset of the reads in the input FASTQ/BAM
    known_reads = set()

    readname_to_haplotype = defaultdict(int)

    haplotype_to_int = {f"H{i}": i for i in range(1, ploidy + 1)}
    haplotype_to_int["none"] = 0

    # No. of reads in the file
    total_reads = 0

    for line in haplolist:
        readname, haplo_name, phaseset, chromosome = line_parser(line)
        total_reads += 1
        try:
            haplo_num = haplotype_to_int[haplo_name]
        except KeyError:
            logger.error(
                "Haplotype name '{haplo_name}' in haplotype list file not recognized; "
                f"must be one of 'none', 'H1', ..., 'H{ploidy}'"
            )
            raise
        if haplo_num == 0:
            if discard_unknown_reads:
                known_reads.add(readname)

            # TODO the below does not save any memory because
            # the moment a nonexisting key in the defaultdict is accessed
            # it is added, so it’ll just use the momery later.

            # Some "trickery" here:
            # Haplotype 0 means untagged;
            # the return value of a defaultdict(int)
            # is zero for unknown keys, so no need to store
            # anything unless "--discard-unknown-reads" is True,
            # in which case we need to know all read names
            continue
        readname_to_haplotype[readname] = haplo_num
        if only_largest_blocks:
            block_sizes[chromosome][phaseset] += 1
            blocks_to_readnames[(chromosome, phaseset)].add(readname)

    # No. of reads that were tagged with a haplotype
    tagged_reads = len(readname_to_haplotype)
    untagged_reads = total_reads - tagged_reads
    logger.info("Total number of reads in haplotag list: %d", total_reads)
    logger.info("Total number of haplo-tagged reads: %d", tagged_reads)
    logger.info("Total number of untagged reads: %d", untagged_reads)

    if discard_unknown_reads:
        known_reads.update(readname_to_haplotype)
        num_known_reads = len(known_reads)
        assert total_reads == num_known_reads, (
            "Total number of reads is not equal to number of known reads:"
            f" {total_reads} != {num_known_reads}"
        )

    if only_largest_blocks:
        selected_reads = select_reads_in_largest_phased_blocks(block_sizes, blocks_to_readnames)
        readname_to_haplotype = defaultdict(
            int, {k: readname_to_haplotype[k] for k in selected_reads}
        )
        num_removed_reads = total_reads - len(readname_to_haplotype)
        logger.info(
            "Number of reads removed / reads not overlapping largest phased blocks: %d",
            num_removed_reads,
        )

    return readname_to_haplotype, known_reads


def _two_column_parser(line):
    cols = line.strip().split("\t")[:2]
    return cols[0], cols[1], None, None


def _four_column_parser(line):
    return line.strip().split("\t")[:4]


def _bam_iterator(bam_file):
    """
    :param bam_file:
    :return:
    """
    for record in bam_file:
        qlen = record.query_length
        if qlen > 0:
            yield record.query_name, qlen, record
        else:
            inferred_qlen = record.infer_query_length()
            if inferred_qlen is not None:
                yield record.query_name, inferred_qlen, record
            else:
                yield record.query_name, 0, record


def _fastq_string_iterator(fastq_file):
    """
    Explicit casting to string because pysam does not seem to
    have a writer for FASTQ files - note that this relies
    on opening all compressed files in "text" mode

    :param fastq_file:
    :return:
    """
    for record in fastq_file:
        yield record.name, len(record.sequence), str(record) + "\n"


def check_haplotag_list_information(haplotag_list, exit_stack):
    """
    Check if the haplotag list file has at least 4 columns
    (assumed to be read name, haplotype, phaseset, chromosome),
    or at least 2 columns (as above). Fails if the haplotag file
    is not tab-separated. Return suitable parser for format

    :param haplotag_list: Tab-separated file with at least 2 or 4 columns
    """
    haplo_list = exit_stack.enter_context(xopen(haplotag_list, threads=0))
    first_line = haplo_list.readline().strip()
    # rewind to make sure a header-less file is processed correctly
    haplo_list.seek(0)
    has_chrom_info = False
    try:
        _, _, _, _ = first_line.split("\t")[:4]
        line_parser = _four_column_parser
    except ValueError:
        try:
            _, _ = first_line.split("\t")[:2]
            line_parser = _two_column_parser
        except ValueError:
            raise ValueError(
                "First line of haplotag list file does not have "
                "at least 2 columns, or it is not tab-separated: {}".format(first_line)
            )
    else:
        has_chrom_info = True
    return haplo_list, has_chrom_info, line_parser


def initialize_io_files(reads_file, outputs, exit_stack):
    potential_fastq_extensions = ["fastq", "fastq.gz", "fastq.gzip" "fq", "fq.gz" "fq.gzip"]
    input_format = detect_file_format(reads_file)
    if input_format is None:
        # TODO: this is a heuristic, need to extend utils::detect_file_format
        if any(reads_file.endswith(ext) for ext in potential_fastq_extensions):
            input_format = "FASTQ"
        else:
            raise ValueError(
                "Undetected file format for input reads. "
                "Expecting BAM or FASTQ (gzipped): {}".format(reads_file)
            )
    elif input_format == "BAM":
        pass
    elif input_format in ["VCF", "CRAM"]:
        raise ValueError(
            "Input file format detected as: {}. Currently, only BAM and FASTQ is supported."
        )
    else:
        # this means somebody changed utils::detect_file_format w/o
        # checking for usage throughout the code
        raise ValueError(
            f"Unexpected file format for input reads: {input_format} - "
            "Expecting BAM or FASTQ (gzipped)"
        )

    output_writers = []
    if input_format == "BAM":
        input_reader = exit_stack.enter_context(
            pysam.AlignmentFile(
                reads_file,
                mode="rb",
                check_sq=False,  # I guess this is needed for unaligned PacBio native files
            )
        )
        input_iter = _bam_iterator

        for outfile in outputs:
            output_writers.append(
                exit_stack.enter_context(
                    pysam.AlignmentFile(
                        os.devnull if outfile is None else outfile, mode="wb", template=input_reader
                    )
                )
            )
    elif input_format == "FASTQ":
        # raw or gzipped is both handled by PySam
        input_reader = exit_stack.enter_context(pysam.FastxFile(reads_file))
        output_mode = "wb"
        if not (reads_file.endswith(".gz") or reads_file.endswith(".gzip")):
            output_mode = "w"
        input_iter = _fastq_string_iterator
        for outfile in outputs:
            output_writers.append(
                exit_stack.enter_context(
                    open(os.devnull, output_mode) if outfile is None else xopen(outfile, "w")
                )
            )
    else:
        # and this means I overlooked something...
        raise ValueError(f"Unhandled file format for input reads: {input_format}")
    return input_reader, input_iter, output_writers


def write_read_length_histogram(length_counts, path):
    # length_counts[0] is for untagged reads
    all_read_lengths = sorted(itertools.chain(*(lc.keys() for lc in length_counts)))
    with xopen(path, "w") as tsv_file:
        columns = (f"count-h{i}" for i in range(1, len(length_counts)))
        print("#length", "count-untagged", *columns, sep="\t", file=tsv_file)
        for rlen in all_read_lengths:
            counts = (lc[rlen] for lc in length_counts)
            print(rlen, *counts, sep="\t", file=tsv_file)


def run_split(
    reads_file,
    list_file,
    output_h1=None,
    output_h2=None,
    outputs=None,
    output_untagged=None,
    add_untagged=False,
    only_largest_block=False,
    discard_unknown_reads=False,
    read_lengths_histogram=None,
):
    if output_h1 or output_h2:
        if outputs:
            raise ValueError("Cannot use output_h1/output_h2 and outputs at the same time")
        outputs = [output_untagged, output_h1, output_h2]
        ploidy = 2
    else:
        ploidy = len(outputs)
        outputs = [output_untagged] + outputs
    del output_untagged
    del output_h1
    del output_h2

    timers = StageTimer()
    timers.start("split-run")

    with ExitStack() as stack:
        timers.start("split-init")

        haplo_list, has_haplo_chrom_info, line_parser = check_haplotag_list_information(
            list_file, stack
        )

        if only_largest_block:
            logger.debug(
                'User selected "--only-largest-block", this requires chromosome '
                "and phaseset information to be present in the haplotag list file."
            )
            if not has_haplo_chrom_info:
                raise ValueError(
                    "The haplotag list file does not contain phaseset and chromosome "
                    "information, which is required to select only reads from the "
                    "largest phased block. Columns 3 and 4 are missing."
                )

        timers.start("split-process-haplotag-list")

        readname_to_haplotype, known_reads = process_haplotag_list_file(
            haplo_list, line_parser, only_largest_block, discard_unknown_reads, ploidy
        )
        if discard_unknown_reads:
            logger.debug(
                "User selected to discard unknown reads, i.e., ignore all reads "
                "that are not part of the haplotag list input file."
            )
            assert (
                len(known_reads) > 0
            ), "No known reads in input set - would discard everything, this is probably wrong"
            missing_reads = len(known_reads)
        else:
            missing_reads = -1

        timers.stop("split-process-haplotag-list")

        input_reader, input_iterator, output_writers = initialize_io_files(
            reads_file, outputs, stack
        )

        timers.stop("split-init")

        histogram_data = [Counter() for _ in outputs]

        # holds count statistics about total processed reads etc.
        read_counter = Counter()

        process_haplotype = [o is not None for o in outputs]
        process_haplotype[0] = process_haplotype[0] or add_untagged

        timers.start("split-iter-input")

        for read_name, read_length, record in input_iterator(input_reader):
            read_counter["total_reads"] += 1
            if discard_unknown_reads and read_name not in known_reads:
                read_counter["unknown_reads"] += 1
                continue
            read_haplotype = readname_to_haplotype[read_name]
            if not process_haplotype[read_haplotype]:
                read_counter["skipped_reads"] += 1
                continue
            histogram_data[read_haplotype][read_length] += 1
            read_counter[read_haplotype] += 1

            output_writers[read_haplotype].write(record)
            if read_haplotype == 0 and add_untagged:
                for writer in output_writers[1:]:
                    writer.write(record)

            if discard_unknown_reads:
                missing_reads -= 1
                if missing_reads == 0:
                    logger.info("All known reads processed - cancel processing...")
                    break

        timers.stop("split-iter-input")

        if read_lengths_histogram is not None:
            timers.start("split-length-histogram")
            write_read_length_histogram(histogram_data, read_lengths_histogram)
            timers.stop("split-length-histogram")

    timers.stop("split-run")

    logger.info("\n== SUMMARY ==")
    logger.info("Total reads processed: {}".format(read_counter["total_reads"]))
    logger.info(f'Number of output reads "untagged": {read_counter[0]}')
    for h in range(1, ploidy + 1):
        logger.info("Number of output reads haplotype %d: %d", h, read_counter[h])
    logger.info("Number of unknown (dropped) reads: {}".format(read_counter["unknown_reads"]))
    logger.info(
        "Number of skipped reads (per user request): {}".format(read_counter["skipped_reads"])
    )

    logger.info(
        "Time for processing haplotag list: {} sec".format(
            round(timers.elapsed("split-process-haplotag-list"), 3)
        )
    )

    logger.info(
        "Time for total initial setup: {} sec".format(round(timers.elapsed("split-init"), 3))
    )

    logger.info(
        "Time for iterating input reads: {} sec".format(
            round(timers.elapsed("split-iter-input"), 3)
        )
    )

    if read_lengths_histogram is not None:
        logger.info(
            "Time for creating histogram output: {} sec".format(
                round(timers.elapsed("split-length-histogram"), 3)
            )
        )

    logger.info("Total run time: {} sec".format(round(timers.elapsed("split-run"), 3)))


def main(args):
    run_split(**vars(args))
