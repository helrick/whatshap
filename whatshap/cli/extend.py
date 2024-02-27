"""
Extend phasing information from haplotagged reads to variants
"""

import logging
import sys
from collections import defaultdict
from contextlib import ExitStack
from typing import List, Optional, Union, Dict, Tuple, FrozenSet, Sequence, TextIO

import numpy
import pysam
from pysam import AlignedSegment

from whatshap.cli import PhasedInputReader, CommandLineError
from whatshap.core import NumericSampleIds, ReadSet, Read, Variant
from whatshap.timer import StageTimer
from whatshap.utils import IndexedFasta
from whatshap.variants import ReadSetReader

from whatshap.vcf import VcfReader, PhasedVcfWriter, VcfError, VariantTable
from whatshap import __version__

logger = logging.getLogger(__name__)


# fmt: off
def add_arguments(parser):
    arg = parser.add_argument
    arg("-o", "--output",
        default=sys.stdout,
        help="Output file. If omitted, use standard output.")
    arg("--reference", "-r", metavar="FASTA",
        help="Reference file. Must be accompanied by .fai index (create with samtools faidx)")
    arg("--gap-threshold", "-g", metavar="GAPTHRESHOLD", default=70, type=int,
        help="Threshold percentage of qualities for assigning phase information to a variant.")
    arg("--cut-poly", "-c", metavar="CUTPOLY", default=-1, type=int,
        help="ignore polymers longer than the cut value.")
    # arg("--regions", dest="regions", metavar="REGION", default=None, action="append",
    #     help="Specify region(s) of interest to limit the tagging to reads/variants "
    #          "overlapping those regions. You can specify a space-separated list of "
    #          "regions in the form of chrom:start-end, chrom (consider entire chromosome), "
    #          "or chrom:start (consider region from this start to end of chromosome).")
    # arg("--ignore-linked-read", default=False, action="store_true",
    #     help="Ignore linkage information stored in BX tags of the reads.")
    # arg("--linked-read-distance-cutoff", "-d", metavar="LINKEDREADDISTANCE",
    #     default=50000, type=int,
    #     help="Assume reads with identical BX tags belong to different read clouds if their "
    #          "distance is larger than LINKEDREADDISTANCE (default: %(default)s).")
    arg("--ignore-read-groups", default=False, action="store_true",
        help="Ignore read groups in BAM/CRAM header and assume all reads come "
             "from the same sample.")
    # arg("--sample", dest="given_samples", metavar="SAMPLE", default=None, action="append",
    #     help="Name of a sample to phase. If not given, all samples in the "
    #          "input VCF are phased. Can be used multiple times.")
    # arg("--output-haplotag-list", dest="haplotag_list", metavar="HAPLOTAG_LIST", default=None,
    #     help="Write assignments of read names to haplotypes (tab separated) to given "
    #          "output file. If filename ends in .gz, then output is gzipped.")
    # arg("--tag-supplementary", default=False, action="store_true",
    #     help="Also tag supplementary alignments. Supplementary alignments are assigned to the "
    #          "same haplotype as the primary alignment (default: only tag primary alignments).")
    arg("--chromosome", dest="chromosomes", metavar="CHROMOSOME", default=[], action="append",
        help="Name of chromosome to phase. If not given, all chromosomes in the "
             "input VCF are phased. Can be used multiple times.")
    arg("variant_file", metavar="VCF", help="VCF file with phased variants "
                                            "(must be gzip-compressed and indexed)")
    arg("alignment_file", metavar="ALIGNMENTS",
        help="BAM/CRAM file with alignments to be tagged by haplotype")


# fmt: on

def run_extend(
    variant_file,
    alignment_file,
    output=None,
    reference: Union[None, bool, str] = False,
    ignore_read_groups: bool = False,
    chromosomes: Optional[List[str]] = None,
    gap_threshold: int = 70,
    cut_poly: int = -1,
    write_command_line_header: bool = True,
    tag: str = "PS",
):
    timers = StageTimer()
    timers.start('extend-run')
    command_line: Optional[str]
    if write_command_line_header:
        command_line = "(whatshap {}) {}".format(__version__, " ".join(sys.argv[1:]))
    else:
        command_line = None
    with (ExitStack() as stack):
        logger.debug("Creating PhasedInputReader")
        phased_input_reader = stack.enter_context(
            PhasedInputReader(
                [alignment_file],
                None if reference is False else reference,
                NumericSampleIds(),
                ignore_read_groups,
                only_snvs=False,
            )
        )
        logger.debug("Creating PhasedVcfWriter")
        try:
            vcf_writer = stack.enter_context(
                PhasedVcfWriter(
                    command_line=command_line,
                    in_path=variant_file,
                    out_file=output,
                    tag=tag,
                )
            )
        except (OSError, VcfError) as e:
            raise CommandLineError(e)

        vcf_reader = stack.enter_context(
            VcfReader(variant_file, phases=True)
        )

        try:
            bam_reader = stack.enter_context(
                pysam.AlignmentFile(
                    alignment_file,
                    reference_filename=reference if reference else None,
                    require_index=True,
                )
            )
        except OSError as err:
            raise CommandLineError(f"Error while loading alignment file {alignment_file}: {err}")

        if ignore_read_groups and len(vcf_reader.samples) > 1:
            raise CommandLineError(
                "When using --ignore-read-groups on a VCF with "
                "multiple samples, --sample must also be used."
            )
        fasta = stack.enter_context(IndexedFasta(reference))
        for variant_table in timers.iterate("parse_vcf", vcf_reader):
            chromosome = variant_table.chromosome
            fasta_chr = fasta[chromosome]
            logger.info(f"Processing chromosome {chromosome}...")
            # logger.info(variant_table.variants)
            if chromosomes and chromosome not in chromosomes:
                logger.info(
                    f"Leaving chromosome {chromosome} unchanged "
                    "(present in VCF, but not requested by --chromosome)")
                with timers("write_vcf"):
                    vcf_writer.write_unchanged(chromosome)
                continue
            for sample in vcf_reader.samples:
                logger.info(f"process sample {sample}")
                reads_to_ht: Dict[str, Tuple[int, int]] = dict()
                with timers("read_bam"):
                    reads, _ = phased_input_reader.read(chromosome, variant_table.variants, sample)
                    for alignment in bam_reader.fetch(chromosome):
                        if alignment.has_tag('PS') and alignment.has_tag('HP'):
                            reads_to_ht[alignment.qname] = (
                                int(alignment.get_tag('PS')) - 1,
                                int(alignment.get_tag('HP')) - 1
                            )
                votes = dict()
                phases = variant_table.phases_of(sample)
                genotypes = variant_table.genotypes_of(sample)

                homozygous = dict()
                change = dict()
                phased = dict()
                homozygous_number = 0
                phased_number = 0
                for variant, (phase, genotype) in zip(variant_table.variants, zip(phases, genotypes)):
                    homozygous[variant.position] = genotype.is_homozygous()
                    phased[variant.position] = phase
                    phased_number += phase is not None
                    homozygous_number += genotype.is_homozygous()
                    change[variant.position] = variant
                logger.info(f'Number of homozygous variants is {homozygous_number}')
                logger.info(f'Number of already phased variants is {phased_number}')
                for read in reads:
                    if read.name not in reads_to_ht:
                        continue
                    ps, ht = reads_to_ht[read.name]
                    for variant in read:
                        if homozygous[variant.position]:
                            continue
                        if variant.position not in votes:
                            votes[variant.position] = dict()
                        if (ps, 0) not in votes[variant.position].keys():
                            votes[variant.position][(ps, 0)] = 0
                            votes[variant.position][(ps, 1)] = 0
                        votes[variant.position][(ps, ht ^ variant.allele)] += variant.quality

                super_reads = [[], []]
                counters = numpy.zeros(101, dtype=numpy.int32)
                components = dict()
                for pos, var in votes.items():
                    lst = list(var.items())
                    lst.sort(key=lambda x: x[-1], reverse=True)
                    (ps1, al1), score1 = lst[0]
                    total = sum(e[-1] for e in lst)
                    components[pos] = ps1
                    q = int(100 * score1 // total)

                    if phased[pos] is None:
                        counters[q] += 1
                    ch = change[pos]
                    # l1 = len(ch.get_ref_allele())
                    # l2 = len(ch.get_alt_allele())
                    # if l1 + l2 > 3:
                    #     continue
                    if ch.is_snv() and phased[pos] is None:
                        continue
                    if q < gap_threshold and phased[pos] is None:
                        continue
                    if cut_poly > 0:
                        j = 1
                        while j + pos + 1 < len(fasta_chr) and j < cut_poly and fasta_chr[pos + j + 1] == fasta_chr[
                            pos + 1]:
                            j = j + 1
                        if j >= cut_poly:
                            continue
                        j = 1
                        while pos - j > 0 and j < cut_poly and fasta_chr[pos - j] == fasta_chr[pos]:
                            j = j + 1
                        if j >= cut_poly:
                            continue
                    super_reads[0].append(Variant(pos, allele=al1, quality=score1))
                    super_reads[1].append(Variant(pos, allele=al1 ^ 1, quality=score1))
                for read in super_reads:
                    read.sort(key=lambda x: x.position)

                vcf_writer.write(chromosome, {sample: super_reads}, {sample: components})


def main(args):
    run_extend(**vars(args))
