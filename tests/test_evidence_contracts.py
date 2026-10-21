"""TP03 complete-source contracts; fixtures contain invented text only."""

from dataclasses import replace
from pathlib import Path
import hashlib
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.evidence_contracts import (
    AcquisitionRecipe, BlockRelation, CompletionCheck, CompletionEvidence,
    DateEvidence, ListPosition, PublicationProvenance, SourceBlock,
    SourceLocator, SourceMember, TableCell, TableGrid, build_source_unit,
    original_publication_event, source_unit_from_dict, source_unit_to_dict,
    validate_source_unit,
)


RAW = hashlib.sha256(b"publisher response").hexdigest()
CHECKS = {
    "publisher_article": ("identity", "body_boundary", "continuation", "structure"),
    "research_paper": ("identity", "all_pages", "structure", "visual_meaning"),
    "discussion": ("identity", "cursor_coverage", "membership_stability", "structure"),
    "publisher_transcript": ("identity", "media_identity", "speech_coverage", "visual_context", "structure"),
}


def completion_for(unit, kind, scope):
    return replace(unit.completion, declared_scope=scope,
                   checks=tuple(CompletionCheck(name, "passed", "fixture-" + name)
                                for name in CHECKS[kind]))


def complete_unit(*, article_id="article-1", text="Alder released PineDB. Migration remains beta.", complete=True):
    block = SourceBlock("body-1", "paragraph", text,
                        SourceLocator("html", {"selector": "article > p:nth-of-type(1)"}))
    recipe = AcquisitionRecipe("controlled_http", "httpx/1", "fetch-v1", RAW,
                               "utf-8", "headers-v1", "extract-v1", ())
    publication = PublicationProvenance("Alder Labs", "https://alder.example/news/pinedb",
                                        (DateEvidence("publisher_publication", "known", "2026-10-14",
                                                      "schema.datePublished", "day", "", "Alder Labs", (), ""),))
    completion = CompletionEvidence("admission-1", "html-v1", complete,
                                    hashlib.sha256(text.encode()).hexdigest(), "whole_article", (), (),
                                    tuple(CompletionCheck(name, "passed", "fixture-" + name)
                                          for name in CHECKS["publisher_article"]))
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
                            SourceLocator("pdf", {"page": 2, "region": "table-1"}),
                            TableGrid(1, 2, (TableCell(0, 0, 1, 1, "Cell A", "header"),
                                             TableCell(0, 1, 1, 1, "Cell B", "header"))))
        text = block.text
        completion = completion_for(unit, "research_paper", "whole_document")
        page = SourceMember("page-2", "page", "2", hashlib.sha256(b"page two").hexdigest())
        recipe = replace(unit.acquisition, members=(page,))
        completion = replace(completion, required_member_ids=("page-2",),
                             observed_member_ids=("page-2",))
        with self.assertRaises(ValueError):
            build_source_unit(unit.article_id, "research_paper", "whole_document",
                              unit.publication, unit.acquisition,
                              replace(completion, required_member_ids=(), observed_member_ids=(),
                                      represented_body_sha256=hashlib.sha256(text.encode()).hexdigest()),
                              (block,))
        replacement = build_source_unit(unit.article_id, "research_paper", "whole_document",
                                        unit.publication, recipe,
                                        replace(completion,
                                                represented_body_sha256=hashlib.sha256(text.encode()).hexdigest()),
                                        (block,))
        self.assertEqual(replacement.passages[0].source_locator.details,
                         {"page": 2, "region": "table-1"})
        with self.assertRaises(ValueError):
            build_source_unit(unit.article_id, "research_paper", "whole_document",
                              unit.publication, recipe,
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
                                       unit.publication, unit.acquisition,
                                       completion_for(unit, "discussion", "whole_thread"),
                                       (discussion_block,))
        self.assertEqual(discussion.passages[0].source_locator.details,
                         {"member_id": "root-1"})
        transcript_block = SourceBlock("cue-1", "transcript_cue", unit.blocks[0].text,
                                       SourceLocator("transcript", {}))
        media_date = DateEvidence("original_media_release", "known", "2026-10-14",
                                  "media-page", "day", "", "Alder Labs", (), "")
        transcript = build_source_unit(unit.article_id, "publisher_transcript", "whole_transcript",
                                       replace(unit.publication, events=(media_date,)), unit.acquisition,
                                       completion_for(unit, "publisher_transcript", "whole_transcript"),
                                       (transcript_block,))
        self.assertEqual(transcript.passages[0].source_locator.details, {})

    def test_nested_list_table_grid_caption_and_qualification_links_survive_version(self):
        unit = complete_unit()
        loc = unit.blocks[0].source_locator
        cells = (TableCell(0, 0, 1, 1, "Name", "header"),
                 TableCell(0, 1, 1, 1, "Value", "header"),
                 TableCell(1, 0, 1, 1, "A", "data"),
                 TableCell(1, 1, 1, 1, "1", "data"))
        blocks = (
            SourceBlock("item-1", "list", "First item", loc, ListPosition(0, 1, None)),
            SourceBlock("item-2", "list", "Nested item", loc, ListPosition(1, 1, "item-1")),
            SourceBlock("table-1", "table", "Name | Value\nA | 1", loc,
                        TableGrid(2, 2, cells)),
            SourceBlock("caption-1", "caption", "Measured results", loc, None,
                        (BlockRelation("caption_of", "table-1"),)),
            SourceBlock("caveat-1", "qualification", "Vendor measurement only", loc, None,
                        (BlockRelation("qualifies", "table-1"),)),
            SourceBlock("code-1", "code", "print('exact')", loc),
            SourceBlock("formula-1", "formula", "x = y + 1", loc),
        )
        body = "\n".join(b.text for b in blocks)
        completion = replace(unit.completion,
                             represented_body_sha256=hashlib.sha256(body.encode()).hexdigest())
        structured = build_source_unit(unit.article_id, unit.source_kind, unit.unit_scope,
                                       unit.publication, unit.acquisition, completion, blocks)
        self.assertEqual([b.block_id for b in structured.blocks],
                         ["item-1", "item-2", "table-1", "caption-1", "caveat-1", "code-1", "formula-1"])
        self.assertEqual(structured.blocks[1].structure.parent_item_id, "item-1")
        self.assertEqual(structured.blocks[2].structure.cells[0].role, "header")
        self.assertEqual(structured.blocks[4].relations[0].target_block_id, "table-1")
        self.assertEqual(validate_source_unit(structured), structured)
        self.assertEqual(source_unit_from_dict(source_unit_to_dict(structured)), structured)
        with self.assertRaises(ValueError):
            build_source_unit(unit.article_id, unit.source_kind, unit.unit_scope,
                              unit.publication, unit.acquisition, completion,
                              (blocks[0], replace(blocks[1], structure=ListPosition(1, 1, "missing")), *blocks[2:]))
        with self.assertRaises(ValueError):
            build_source_unit(unit.article_id, unit.source_kind, unit.unit_scope,
                              unit.publication, unit.acquisition, completion,
                              (*blocks[:4], replace(blocks[4], relations=(BlockRelation("qualifies", "missing"),)),
                               *blocks[5:]))
        with self.assertRaises(ValueError):
            build_source_unit(unit.article_id, unit.source_kind, unit.unit_scope,
                              unit.publication, unit.acquisition, completion,
                              (*blocks[:2], replace(blocks[2], structure=TableGrid(2, 2, cells[:-1])), *blocks[3:]))

    def test_transcript_original_media_release_is_distinct_from_newer_transcript_and_retrieval(self):
        unit = complete_unit()
        events = (
            DateEvidence("original_media_release", "known", "2025-03-01", "media-page", "day", "", "Alder Labs", (), ""),
            DateEvidence("transcript_publication", "known", "2026-10-14", "transcript-page", "day", "", "Alder Labs", (), ""),
            DateEvidence("retrieval", "known", "2026-10-21T09:00:00+08:00", "http-response", "instant", "+08:00", "client", (), ""),
            DateEvidence("revision", "unknown", "", "", "unknown", "", "unknown", (), "not supplied"),
        )
        transcript = build_source_unit(unit.article_id, "publisher_transcript", "whole_transcript",
                                       replace(unit.publication, events=events), unit.acquisition,
                                       completion_for(unit, "publisher_transcript", "whole_transcript"),
                                       (SourceBlock("cue-1", "transcript_cue", unit.blocks[0].text,
                                                    SourceLocator("transcript", {})),))
        self.assertEqual(original_publication_event(transcript).raw_value, "2025-03-01")
        unknown = replace(events[0], status="unknown", raw_value="", precision="unknown",
                          source_location="", explanation="publisher release unavailable")
        unknown_unit = build_source_unit(unit.article_id, "publisher_transcript", "whole_transcript",
                                         replace(unit.publication, events=(unknown, *events[1:])),
                                         unit.acquisition,
                                         completion_for(unit, "publisher_transcript", "whole_transcript"),
                                         transcript.blocks)
        self.assertIsNone(original_publication_event(unknown_unit))
        with self.assertRaises(ValueError):
            build_source_unit(unit.article_id, "publisher_transcript", "whole_transcript",
                              replace(unit.publication, events=events[1:]), unit.acquisition,
                              completion_for(unit, "publisher_transcript", "whole_transcript"),
                              transcript.blocks)

    def test_incomplete_member_coverage_or_failed_required_check_is_not_admitted(self):
        unit = complete_unit()
        member = SourceMember("page-1", "page", "1", hashlib.sha256(b"page one").hexdigest())
        recipe = replace(unit.acquisition, members=(member,))
        required = replace(unit.completion, required_member_ids=("page-1",), observed_member_ids=())
        with self.assertRaises(ValueError):
            build_source_unit(unit.article_id, unit.source_kind, unit.unit_scope,
                              unit.publication, recipe, required, unit.blocks)
        failed = replace(unit.completion, checks=(CompletionCheck("identity", "failed", "bad"),
                                                  *unit.completion.checks[1:]))
        with self.assertRaises(ValueError):
            build_source_unit(unit.article_id, unit.source_kind, unit.unit_scope,
                              unit.publication, unit.acquisition, failed, unit.blocks)

    def test_table_span_and_leading_qualification_preserve_source_order(self):
        unit = complete_unit()
        loc = unit.blocks[0].source_locator
        grid = TableGrid(2, 2, (
            TableCell(0, 0, 1, 2, "Metrics", "header"),
            TableCell(1, 0, 1, 1, "A", "data"),
            TableCell(1, 1, 1, 1, "1", "data")))
        blocks = (
            SourceBlock("caveat", "qualification", "Vendor test only", loc, None,
                        (BlockRelation("qualifies", "table"),)),
            SourceBlock("table", "table", "Metrics\nA | 1", loc, grid),
        )
        completion = replace(unit.completion,
                             represented_body_sha256=hashlib.sha256(
                                 "\n".join(b.text for b in blocks).encode()).hexdigest())
        structured = build_source_unit(unit.article_id, unit.source_kind, unit.unit_scope,
                                       unit.publication, unit.acquisition, completion, blocks)
        self.assertEqual(structured.blocks[0].relations[0].target_block_id, "table")
        self.assertEqual(structured.blocks[1].structure.cells[0].column_span, 2)
        self.assertEqual([p.block_id for p in structured.passages], ["caveat", "table"])

    def test_separate_list_groups_can_restart_order_without_losing_parent_links(self):
        unit = complete_unit()
        loc = unit.blocks[0].source_locator
        blocks = (
            SourceBlock("a", "list", "Group A", loc, ListPosition(0, 1, None, "list-a")),
            SourceBlock("a-child", "list", "Child A", loc, ListPosition(1, 1, "a", "list-a")),
            SourceBlock("b", "list", "Group B", loc, ListPosition(0, 1, None, "list-b")),
        )
        completion = replace(unit.completion,
                             represented_body_sha256=hashlib.sha256(
                                 "\n".join(b.text for b in blocks).encode()).hexdigest())
        self.assertEqual(build_source_unit(unit.article_id, unit.source_kind, unit.unit_scope,
                                           unit.publication, unit.acquisition, completion,
                                           blocks).blocks[2].structure.list_id, "list-b")

    def test_conflicted_original_publication_retains_candidates_without_using_retrieval(self):
        unit = complete_unit()
        conflict = DateEvidence("publisher_publication", "conflict", "", "publisher-pages",
                                "unknown", "", "Alder Labs", ("2026-10-13", "2026-10-14"),
                                "two publisher locations disagree")
        retrieval = DateEvidence("retrieval", "known", "2026-10-21T09:00:00+08:00",
                                 "response", "instant", "+08:00", "client", (), "")
        contested = build_source_unit(unit.article_id, unit.source_kind, unit.unit_scope,
                                      replace(unit.publication, events=(conflict, retrieval)),
                                      unit.acquisition, unit.completion, unit.blocks)
        self.assertIsNone(original_publication_event(contested))
        self.assertEqual(contested.publication.events[0].conflicting_values,
                         ("2026-10-13", "2026-10-14"))
        self.assertNotEqual(contested.version_id, unit.version_id)

    def test_client_retrieval_owner_cannot_claim_original_publisher_publication(self):
        unit = complete_unit()
        falsely_owned = replace(unit.publication.events[0], owner="client")
        with self.assertRaises(ValueError):
            build_source_unit(unit.article_id, unit.source_kind, unit.unit_scope,
                              replace(unit.publication, events=(falsely_owned,)),
                              unit.acquisition, unit.completion, unit.blocks)

    def test_declared_discussion_cursor_and_continuation_require_observed_members(self):
        unit = complete_unit()
        root = SourceBlock("root", "paragraph", unit.blocks[0].text,
                           SourceLocator("discussion", {"member_id": "root"}))
        members = (
            SourceMember("cursor-1", "cursor", "next=abc", hashlib.sha256(b"page one").hexdigest()),
            SourceMember("continuation-2", "continuation", "next=def",
                         hashlib.sha256(b"page two").hexdigest()),
        )
        recipe = replace(unit.acquisition, members=members)
        completion = replace(completion_for(unit, "discussion", "whole_thread"),
                             required_member_ids=("cursor-1", "continuation-2"),
                             observed_member_ids=("cursor-1", "continuation-2"))
        complete = build_source_unit(unit.article_id, "discussion", "whole_thread",
                                     unit.publication, recipe, completion, (root,))
        self.assertEqual([m.kind for m in complete.acquisition.members], ["cursor", "continuation"])
        with self.assertRaises(ValueError):
            build_source_unit(unit.article_id, "discussion", "whole_thread",
                              unit.publication, recipe,
                              replace(completion, observed_member_ids=("cursor-1",)), (root,))

    def test_discussion_reply_cannot_point_to_missing_parent(self):
        unit = complete_unit()
        reply = SourceBlock("reply", "paragraph", unit.blocks[0].text,
                            SourceLocator("discussion", {"member_id": "reply-1",
                                                         "parent_id": "absent-root"}))
        with self.assertRaises(ValueError):
            build_source_unit(unit.article_id, "discussion", "whole_thread",
                              unit.publication, unit.acquisition,
                              completion_for(unit, "discussion", "whole_thread"), (reply,))


if __name__ == "__main__":
    unittest.main()
