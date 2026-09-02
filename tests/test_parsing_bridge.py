import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "parsing" / "extract.py"
SPEC = importlib.util.spec_from_file_location("parsing_extract", MODULE_PATH)
extract = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(extract)


def test_parser_root_can_be_loaded_from_current_checkout():
    parser_root = Path(__file__).resolve().parents[3]
    builder = extract.load_lifecycle_builder(parser_root)
    assert builder.__name__ == "build_source_lifecycle"


def test_options_are_preserved_by_argument_handling():
    args = extract.parse_args(["--options-json", '{"language":"zh"}'])
    assert args.options_json == '{"language":"zh"}'
