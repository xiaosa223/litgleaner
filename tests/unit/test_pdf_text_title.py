from litgleaner.adapters.parsers.pdf_text import _is_heading, _title


def _line(text: str, top: float, height: float) -> tuple[str, tuple[float, float, float, float]]:
    return text, (100.0, top, 900.0, top + height)


def test_title_is_the_tallest_adjacent_lines_with_ligatures_normalized():
    lines = [
        _line("“Why Should I Trust You?”", 100, 30),
        _line("Explaining the Predictions of Any Classiﬁer", 135, 29),
        _line("Marco Tulio Ribeiro", 180, 14),
        _line("Despite widespread adoption, machine learning models remain black boxes.", 300, 12),
        _line("Understanding the reasons behind predictions is important.", 315, 12),
    ]
    assert _title(lines) == "“Why Should I Trust You?” Explaining the Predictions of Any Classifier"


def test_no_title_when_first_page_has_no_larger_text_or_only_section_names():
    flat = [_line(f"body text line number {n}", 100 + 15 * n, 12) for n in range(6)]
    assert _title(flat) is None
    assert _title([_line("ABSTRACT", 100, 30), *flat]) is None
    assert _title([("No box line", None)]) is None


def test_formula_and_number_lines_are_not_headings():
    assert _is_heading("1. Introduction")
    assert _is_heading("III. RANDOM MATRIX THEORY FOR ANOMALY")
    assert not _is_heading("33 34 ts = 1~ 1000 0.2ts =")
    assert not _is_heading("I. During ts = 500 ~ 1000,")
    assert not _is_heading("20 ts = 1~ 500 20ts = 501~")
    assert not _is_heading("1 2 3 4 5 6 7 8")
    assert not _is_heading("1 ∼ 4 are shown in Figure 9, where κMSR and η are")
    assert not _is_heading("8. In the experiments, the size of the moving window was set")
    assert not _is_heading("20 newsgroups dataset later on). The insights given by expla-")
    assert not _is_heading("1 Department of Electrical Engineering,Center for Big Data")
    assert not _is_heading("2\u00a0（郑州大学网络空间安全学院\u3000郑州\u3000450001）")
    assert _is_heading("3.3\u3000基于符号执行的漏洞自动利用")
    assert _is_heading("5.2 Are explanations faithful to the model?")
    assert _is_heading("B. Case Study on Comparison with Other Approaches")
    # IEEE 的罗马数字一级标题是全大写；接小写句子的是被折到行首的“Table III”
    assert not _is_heading("III. The detection curves in our SA approach increase")
    assert _is_heading("III. R ANDOM MATRIX THEORY FOR ANOMALY")
    assert _is_heading("V. C ONCLUSION")
    assert not _is_heading("P. Simard, and J. Suh. Modeltracker: Redesigning")
    assert not _is_heading("R. Salakhutdinov, R. Zemel, and Y. Bengio")
    assert _is_heading("A. Spatio-Temporal Data Formulation")
    assert not _is_heading("N. Elhadad. Intelligible models for healthcare")
    assert _is_heading("B. Macroscopic Indicator")
    assert _is_heading("A. Case Study on L = 1")
