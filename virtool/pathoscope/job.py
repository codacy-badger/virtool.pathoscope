"""
Functions and job classes for sample analysis.

"""
import json
import os
import shlex
import shutil

import pymongo
import pymongo.errors
from virtool.job import Job

import virtool.pathoscope.pathoscope as pathoscope


class PathoscopeBowtie(Job):
    """
    A base class for all analysis job objects. Functions include:

    - establishing synchronous database connection
    - extracting task args to attributes
    - retrieving the sample and host documents
    - calculating the sample read count
    - constructing paths used by all subclasses

    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._stage_list = [
            self.mk_analysis_dir,
            self.map_otus,
            self.generate_isolate_fasta,
            self.build_isolate_index,
            self.map_isolates,
            self.map_subtraction,
            self.subtract_mapping,
            self.pathoscope,
            self.import_results,
            self.cleanup_indexes

        ]

    def check_db(self):
        """
        Get some initial information from the database that will be required during the course of the job.

        """
        self.params = {
            # The document id for the sample being analyzed. and the analysis document the results will be committed to.
            "sample_id": self.task_args["sample_id"],

            # The document id for the reference to analyze against.
            "ref_id": self.task_args["ref_id"],

            # The document id for the analysis being run.
            "analysis_id": self.task_args["analysis_id"],
        }

        # The parent folder for all data associated with the sample
        sample_path = os.path.join(self.settings["data_path"], "samples", self.params["sample_id"])

        self.params.update({
            # The path to the directory where all analysis result files will be written.
            "analysis_path": os.path.join(sample_path, "analysis", self.params["analysis_id"]),

            "index_path":  os.path.join(
                self.settings["data_path"],
                "references",
                self.params["ref_id"],
                self.task_args["index_id"],
                "reference"
            )
        })

        # Get the complete sample document from the database.
        sample = self.db.samples.find_one(self.params["sample_id"])

        read_paths = [os.path.join(sample_path, "reads_1.fastq")]

        paired = sample.get("paired", None)

        if paired is None:
            paired = len(sample["files"]) == 2

        if paired:
            read_paths.append(os.path.join(sample_path, "reads_2.fastq"))

        self.params.update({
            "paired": paired,

            #: The number of reads in the sample library. Assigned after database connection is made.
            "read_count": int(sample["quality"]["count"]),
            "read_paths": read_paths,
            "subtraction_path": os.path.join(
                self.settings["data_path"],
                "subtractions",
                sample["subtraction"]["id"].lower().replace(" ", "_"),
                "reference"
            )
        })

    def mk_analysis_dir(self):
        """
        Make a directory for the analysis in the sample/analysis directory.

        """
        os.mkdir(self.params["analysis_path"])

    def map_otus(self):
        """
        Using ``bowtie2``, maps reads to the main otu reference. This mapping is used to identify candidate otus.

        """
        command = [
            "bowtie2",
            "-p", str(self.proc),
            "--no-unal",
            "--local",
            "--score-min", "L,20,1.0",
            "-N", "0",
            "-L", "15",
            "-x", self.params["index_path"],
            "-U", ",".join(self.params["read_paths"])
        ]

        to_otus = set()

        def stdout_handler(line):
            line = line.decode()

            if line[0] == "#" or line[0] == "@":
                return

            fields = line.split("\t")

            # Bitwise FLAG - 0x4: segment unmapped
            if int(fields[1]) & 0x4 == 4:
                return

            ref_id = fields[2]

            if ref_id == "*":
                return

            # Skip if the p_score does not meet the minimum cutoff.
            if pathoscope.find_sam_align_score(fields) < 0.01:
                return

            to_otus.add(ref_id)

        self.run_subprocess(command, stdout_handler=stdout_handler)

        self.intermediate["to_otus"] = to_otus

    def generate_isolate_fasta(self):
        """
        Identifies otu hits from the initial default otu mapping.

        """
        self.intermediate["otu_dict"] = self.task_args["otu_dict"]

        self.intermediate["sequence_otu_map"] = {item[0]: item[1] for item in self.task_args["sequence_otu_map"]}

        fasta_path = os.path.join(self.params["analysis_path"], "isolate_index.fa")

        sequence_ids = list(self.intermediate["to_otus"])

        ref_lengths = dict()

        # Get the database documents for the sequences
        with open(fasta_path, "w") as handle:
            # Iterate through each otu id referenced by the hit sequence ids.
            for otu_id in self.db.sequences.distinct("otu_id", {"_id": {"$in": sequence_ids}}):
                # Write all of the sequences for each otu to a FASTA file.
                for document in self.db.sequences.find({"otu_id": otu_id}, ["sequence"]):
                    handle.write(">{}\n{}\n".format(document["_id"], document["sequence"]))
                    ref_lengths[document["_id"]] = len(document["sequence"])

        del self.intermediate["to_otus"]

        self.intermediate["ref_lengths"] = ref_lengths

    def build_isolate_index(self):
        """
        Build an index with ``bowtie2-build`` from the FASTA file generated by
        :meth:`Pathoscope.generate_isolate_fasta`.

        """
        command = [
            "bowtie2-build",
            os.path.join(self.params["analysis_path"], "isolate_index.fa"),
            os.path.join(self.params["analysis_path"], "isolates")
        ]

        self.run_subprocess(command)

    def map_isolates(self):
        """
        Using ``bowtie2``, map the sample reads to the index built using :meth:`.build_isolate_index`.

        """
        command = [
            "bowtie2",
            "-p", str(self.proc - 1),
            "--no-unal",
            "--local",
            "--score-min", "L,20,1.0",
            "-N", "0",
            "-L", "15",
            "-k", "100",
            "--al", os.path.join(self.params["analysis_path"], "mapped.fastq"),
            "-x", os.path.join(self.params["analysis_path"], "isolates"),
            "-U", ",".join(self.params["read_paths"])
        ]

        with open(os.path.join(self.params["analysis_path"], "to_isolates.vta"), "w") as f:
            def stdout_handler(line, p_score_cutoff=0.01):
                line = line.decode()

                if line[0] == "@" or line == "#":
                    return

                fields = line.split("\t")

                # Bitwise FLAG - 0x4 : segment unmapped
                if int(fields[1]) & 0x4 == 4:
                    return

                ref_id = fields[2]

                if ref_id == "*":
                    return

                p_score = pathoscope.find_sam_align_score(fields)

                # Skip if the p_score does not meet the minimum cutoff.
                if p_score < p_score_cutoff:
                    return

                f.write(",".join([
                    fields[0],  # read_id
                    ref_id,
                    fields[3],  # pos
                    str(len(fields[9])),  # length
                    str(p_score)
                ]) + "\n")

            self.run_subprocess(command, stdout_handler=stdout_handler)

    def map_subtraction(self):
        """
        Using ``bowtie2``, map the reads that were successfully mapped in :meth:`.map_isolates` to the subtraction host
        for the sample.

        """
        command = [
            "bowtie2",
            "--local",
            "-N", "0",
            "-p", str(self.proc - 1),
            "-x", shlex.quote(self.params["subtraction_path"]),
            "-U", os.path.join(self.params["analysis_path"], "mapped.fastq")
        ]

        to_subtraction = dict()

        def stdout_handler(line):
            line = line.decode()

            if line[0] == "@" or line == "#":
                return

            fields = line.split("\t")

            # Bitwise FLAG - 0x4 : segment unmapped
            if int(fields[1]) & 0x4 == 4:
                return

            # No ref_id assigned.
            if fields[2] == "*":
                return

            to_subtraction[fields[0]] = pathoscope.find_sam_align_score(fields)

        self.run_subprocess(command, stdout_handler=stdout_handler)

        self.intermediate["to_subtraction"] = to_subtraction

    def subtract_mapping(self):
        subtracted_count = pathoscope.subtract(
            self.params["analysis_path"],
            self.intermediate["to_subtraction"]
        )

        del self.intermediate["to_subtraction"]

        self.results["subtracted_count"] = subtracted_count

    def pathoscope(self):
        """
        Run the Pathoscope reassignment algorithm. Tab-separated output is written to ``pathoscope.tsv``. Results are
        also parsed and saved to :attr:`intermediate`.

        """
        vta_path = os.path.join(self.params["analysis_path"], "to_isolates.vta")
        reassigned_path = os.path.join(self.params["analysis_path"], "reassigned.vta")

        (
            best_hit_initial_reads,
            best_hit_initial,
            level_1_initial,
            level_2_initial,
            best_hit_final_reads,
            best_hit_final,
            level_1_final,
            level_2_final,
            init_pi,
            pi,
            refs,
            reads
        ) = run_patho(vta_path, reassigned_path)

        read_count = len(reads)

        report = pathoscope.write_report(
            os.path.join(self.params["analysis_path"], "report.tsv"),
            pi,
            refs,
            read_count,
            init_pi,
            best_hit_initial,
            best_hit_initial_reads,
            best_hit_final,
            best_hit_final_reads,
            level_1_initial,
            level_2_initial,
            level_1_final,
            level_2_final
        )

        self.intermediate["coverage"] = pathoscope.calculate_coverage(
            reassigned_path,
            self.intermediate["ref_lengths"]
        )

        self.results = {
            "ready": True,
            "read_count": read_count,
            "diagnosis": list()
        }

        for ref_id, hit in report.items():
            # Get the otu info for the sequence id.
            otu = self.intermediate["otu_dict"][self.intermediate["sequence_otu_map"][ref_id]]

            # Raise exception if otu is ``False`` (meaning the otu had no ``last_indexed_version`` field).
            if not otu:
                raise ValueError("Document has no last_indexed_version field.")

            hit["id"] = ref_id

            # Attach "otu" (id, version) to the hit.
            hit["otu"] = otu

            # Get the coverage for the sequence.
            hit_coverage = self.intermediate["coverage"][ref_id]

            # Attach coverage list to hit dict.
            hit["align"] = hit_coverage

            # Calculate coverage and attach to hit.
            hit["coverage"] = round(1 - hit_coverage.count(0) / len(hit_coverage), 3)

            # Calculate depth and attach to hit.
            hit["depth"] = round(sum(hit_coverage) / len(hit_coverage))

            self.results["diagnosis"].append(hit)

    def import_results(self):
        """
        Commits the results to the database. Data includes the output of Pathoscope, final mapped read count,
        and viral genome coverage maps.

        Once the import is complete, :meth:`cleanup_index_files` is called to remove
        any otu indexes that may become unused when this analysis completes.

        """
        analysis_id = self.params["analysis_id"]

        try:
            self.db.analyses.update_one({"_id": analysis_id}, {
                "$set": self.results
            })
        except pymongo.errors.DocumentTooLarge:
            with open(os.path.join(self.params["analysis_path"], "pathoscope.json"), "w") as f:
                json_string = json.dumps(self.results)
                f.write(json_string)

            self.db.analyses.update_one({"_id": analysis_id}, {
                "$set": {
                    "diagnosis": "file",
                    "ready": True
                }
            })

            self.dispatch("analyses", "update", [analysis_id])

    def cleanup(self):
        """
        Remove the analysis document and the analysis files. Dispatch the removal op. Recalculate and update the
        algorithm tags for the sample document.

        """
        self.db.analyses.delete_one({"_id": self.params["analysis_id"]})

        try:
            shutil.rmtree(self.params["analysis_path"])
        except FileNotFoundError:
            pass

    def cleanup_indexes(self):
        pass


def run_patho(vta_path, reassigned_path):
    u, nu, refs, reads = pathoscope.build_matrix(vta_path)

    best_hit_initial_reads, best_hit_initial, level_1_initial, level_2_initial = pathoscope.compute_best_hit(
        u,
        nu,
        refs,
        reads
    )

    init_pi, pi, _, nu = pathoscope.em(u, nu, refs, 50, 1e-7, 0, 0)

    best_hit_final_reads, best_hit_final, level_1_final, level_2_final = pathoscope.compute_best_hit(
        u,
        nu,
        refs,
        reads
    )

    pathoscope.rewrite_align(u, nu, vta_path, 0.01, reassigned_path)

    return (
        best_hit_initial_reads,
        best_hit_initial,
        level_1_initial,
        level_2_initial,
        best_hit_final_reads,
        best_hit_final,
        level_1_final,
        level_2_final,
        init_pi,
        pi,
        refs,
        reads
    )
