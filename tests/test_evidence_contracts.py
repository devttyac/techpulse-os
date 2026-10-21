"""TP03 complete-source contracts; fixtures contain invented text only."""

from dataclasses import replace
from pathlib import Path
import hashlib
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.evidence_contracts import (
    AcquisitionRecipe, CompletionEvidence, PublicationProvenance,
    SourceBlock, SourceLocator, build_source_unit, validate_source_unit,
)


RAW = hashlib.sha256(b"publisher response").hexdigest()


def complete_unit(*, article_id="article-1", text="Alder released PineDB. Migration remains beta.", complete=True):
    block = SourceBlock("body-1", "paragraph", text,
                        SourceLocator("html", {"selector": "article > p:nth-of-type(1)"}))
    recipe = AcquisitionRecipe("controlled_http", "httpx/1", "fetch-v1", RAW,
                               "utf-8", "headers-v1", "extract-v1", ())
    publication = PublicationProvenance("Alder Labs", "https://alder.example/news/pinedb",
                                        "2026-10-14", "day", "publisher-page")
    completion = CompletionEvidence("admission-1", "html-v1", complete,
                                    hashlib.sha256(text.encode()).hexdigest())
    return build_source_unit(article_id, "publisher_article", "whole_article",
                             publication, recipe, completion, (block,))


class CompleteSourceContractTests(unittest.TestCase):
    def test_version_and_passage_bind_exact_structure_and_source_locator(self):
        unit = complete_unit()
        self.assertEqual(validate_source_unit(unit), unit)
        self.assertEqual(len(unit.passages), 1)
        passage = unit.passages[0]
        self.assertEqual(passage.text, unit.blocks[0].text)
        self.assertEqual((passage.start, passage.end), (0, len(passage.text)))
        self.assertEqual(passage.source_locator, unit.blocks[0].source_locator)
        self.assertIn(unit.version_id, passage.passage_id)
        self.assertEqual(complete_unit().version_id, unit.version_id)
        self.assertNotEqual(complete_unit(text="Alder edited its article.").version_id,
                            unit.version_id)
        self.assertNotEqual(complete_unit(article_id="article-2").version_id,
                            unit.version_id)

    def test_incomplete_or_raw_only_source_never_validates(self):
        with self.assertRaises(ValueError):
            complete_unit(complete=False)
        unit = complete_unit()
        with self.assertRaises(ValueError):
            build_source_unit(unit.article_id, unit.source_kind, unit.unit_scope,
                              unit.publication, unit.acquisition, unit.completion, ())

    def test_modified_passage_or_unverified_completion_cannot_claim_saved_version(self):
        unit = complete_unit()
        with self.assertRaises(ValueError):
            validate_source_unit(replace(unit, passages=(replace(unit.passages[0], text="other"),)))
        with self.assertRaises(ValueError):
            validate_source_unit(replace(unit, completion=replace(unit.completion,
                                        represented_body_sha256="0" * 64)))

    def test_supported_non_html_locator_keeps_exact_source_coordinates(self):
        unit = complete_unit()
        block = SourceBlock("page-2", "table", "Cell A | Cell B",
                            SourceLocator("pdf", {"page": 2, "region": "table-1"}))
        text = block.text
        replacement = build_source_unit(unit.article_id, "research_paper", "whole_document",
                                        unit.publication, unit.acquisition,
                                        replace(unit.completion,
                                                represented_body_sha256=hashlib.sha256(text.encode()).hexdigest()),
                                        (block,))
        self.assertEqual(replacement.passages[0].source_locator.details,
                         {"page": 2, "region": "table-1"})
        with self.assertRaises(ValueError):
            build_source_unit(unit.article_id, "research_paper", "whole_document",
                              unit.publication, unit.acquisition,
                              replacement.completion,
                              (replace(block, source_locator=SourceLocator("pdf", {"page": 2})),))

    def test_repository_or_transcript_cannot_be_relabelled_as_news_article(self):
        unit = complete_unit()
        repository_block = replace(unit.blocks[0], source_locator=SourceLocator(
            "repository", {"commit": "abc123", "path": "src/main.py",
                           "start_line": 1, "end_line": 3}))
        with self.assertRaises(ValueError):
            build_source_unit(unit.article_id, "publisher_article", "whole_article",
                              unit.publication, unit.acquisition, unit.completion,
                              (repository_block,))

    def test_root_discussion_and_transcript_without_cues_preserve_absence(self):
        unit = complete_unit()
        discussion_block = SourceBlock("thread-1", "paragraph", unit.blocks[0].text,
                                       SourceLocator("discussion", {"member_id": "root-1"}))
        discussion = build_source_unit(unit.article_id, "discussion", "whole_thread",
                                       unit.publication, unit.acquisition, unit.completion,
                                       (discussion_block,))
        self.assertEqual(discussion.passages[0].source_locator.details,
                         {"member_id": "root-1"})
        transcript_block = SourceBlock("cue-1", "transcript_cue", unit.blocks[0].text,
                                       SourceLocator("transcript", {}))
        transcript = build_source_unit(unit.article_id, "publisher_transcript", "whole_transcript",
                                       unit.publication, unit.acquisition, unit.completion,
                                       (transcript_block,))
        self.assertEqual(transcript.passages[0].source_locator.details, {})


if __name__ == "__main__":
    unittest.main()
