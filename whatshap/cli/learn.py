"""
Generate sequencing technology specific error profiles
"""
import logging
import pysam
import pyfaidx
from whatshap.core import Caller
from pysam import VariantFile


logger = logging.getLogger(__name__)


def add_arguments(parser):
    arg = parser.add_argument
    arg("--reference", "-r", metavar="FASTA", help="Reference genome", required=True)
    arg("--bam", metavar="BAM", help="Aligned reads", required=True)
    arg("--vcf", metavar="VCF", help="Variants", required=True)
    arg("-k", "--kmer", dest="k", metavar="K", help="k-mer size", type=int, required=True)
    arg(
        "--window",
        metavar="WINDOW",
        help="Ignore this many bases on the left and right of each variant position",
        type=int,
        required=True,
    )
    arg("--output", "-o", metavar="OUT", help="Output file with kmer-pair counts", required=True)


def run_learn(reference, bam, vcf, k: int, window: int, output):
    with (
        pyfaidx.Fasta(reference, as_raw=True) as fasta,
        pysam.AlignmentFile(bam) as bamfile,
        VariantFile(vcf) as vcf_in,
    ):
        variantslist = []
        call = 0
        for variant in vcf_in.fetch():
            variantslist.append((variant.pos, len(variant.ref)))
        encoded_references = {}
        chromosome = None
        open(output, "w").close()
        output_c = str(output).encode("UTF-8")
        for bam_alignment in bamfile:
            if bam_alignment.is_unmapped or bam_alignment.query_alignment_sequence is None:
                continue
            if bam_alignment.reference_name != chromosome:
                chromosome = bam_alignment.reference_name
                if chromosome in encoded_references:
                    caller = Caller(encoded_references[chromosome], k, window)
                else:
                    ref = fasta[chromosome]
                    encoded_references[chromosome] = str(ref).encode("UTF-8")
                    caller = Caller(encoded_references[chromosome], k, window)
            if call == 0:
                caller.all_variants(variantslist)
                call = 1
            else:
                pass
            caller.add_read(
                bam_alignment.pos,
                bam_alignment.cigartuples,
                str(bam_alignment.query_alignment_sequence).encode("UTF-8"),
                output_c,
            )
        caller.final_pop(output_c)


def main(args):
    run_learn(**vars(args))
