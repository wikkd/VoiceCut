"""字幕推理 + 音色识别联合判定的纯函数单元测试（不加载任何模型）。

覆盖 speakers.py 的三个新机制：
- _viterbi_labels：对话连续性先验（弱翻转被吸收、强证据保留）
- _window_runs：连续段合并（mixed 证据，抗散落噪声票）
- _apportion_text：mixed 拆分的句读切点（不在词中间断开）
"""
from app.speakers import (
    _apportion_text,
    _viterbi_labels,
    _window_runs,
)


A = [1.0, 0.0]
B = [0.0, 1.0]
CENTROIDS = {"说话人1": A, "说话人2": B}


def _cos_ish(a, b):
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return sum(x * y for x, y in zip(a, b)) / (na * nb + 1e-9)


def _nearest(e):
    sims = {lb: _cos_ish(e, c) for lb, c in CENTROIDS.items()}
    return max(sims, key=sims.get)


class TestViterbi:
    def test_weak_outlier_snaps_back(self):
        # 序列 A A [弱B] A A：窗口噪声把中间行投给 B，但证据差距小于
        # 两次切换代价 → Viterbi 吸回 A（旧逻辑会把这行拆给另一个"人"）
        a = [0.95, 0.31]
        weak_b = [0.48, 0.52]           # nearest = B，但 margin ~0.057 < 2*0.05
        embs = [a, a, weak_b, a, a]
        init = [_nearest(e) for e in embs]
        assert init[2] == "说话人2"
        out = _viterbi_labels(embs, init, CENTROIDS, 0.05)
        assert out == ["说话人1"] * 5

    def test_strong_voice_survives(self):
        # 序列 A [强B] A：强证据的第二人保留自己的标签（不能被连续性抹掉）
        a = [0.95, 0.31]
        strong_b = [0.05, 0.9986]
        embs = [a, strong_b, a]
        init = [_nearest(e) for e in embs]
        assert init == ["说话人1", "说话人2", "说话人1"]
        out = _viterbi_labels(embs, init, CENTROIDS, 0.05)
        assert out == ["说话人1", "说话人2", "说话人1"]

    def test_monologue_block_merges(self):
        # A 的长段中夹两行弱 B 噪声 → 全并回 A（一人拆多人的主场景）。
        # 每行证据 margin (~0.028) 低于切换代价 0.05 → 2 行累计也盖不过 2 次切换。
        a = [0.95, 0.31]
        weak_b = [0.49, 0.51]
        embs = [a, a, weak_b, weak_b, a, a, a]
        init = [_nearest(e) for e in embs]
        assert init[2] == "说话人2" and init[3] == "说话人2"
        out = _viterbi_labels(embs, init, CENTROIDS, 0.05)
        assert set(out) == {"说话人1"}

    def test_two_line_consistent_evidence_survives(self):
        # 连续两行 margin ~0.11 的一致证据 = 真实换人（对话轮替），不能被抹掉：
        # 2×0.113 − 2×0.05 > 0 → Viterbi 保留 B
        a = [0.95, 0.31]
        b = [0.46, 0.54]
        embs = [a, a, b, b, a, a]
        init = [_nearest(e) for e in embs]
        out = _viterbi_labels(embs, init, CENTROIDS, 0.05)
        assert out == ["说话人1", "说话人1", "说话人2", "说话人2",
                       "说话人1", "说话人1"]

    def test_no_embedding_keeps_init(self):
        out = _viterbi_labels([None, None], ["说话人2", "说话人1"], CENTROIDS)
        assert out == ["说话人2", "说话人1"]


class TestWindowRuns:
    def test_contiguous_merge(self):
        eA = [0.98, 0.02]
        eB = [0.02, 0.98]
        vecs = [(0.0, 0.5, eA), (0.5, 1.0, eA), (1.0, 1.5, eB),
                (1.5, 2.0, eB), (2.0, 2.5, eA)]
        runs = _window_runs(vecs, CENTROIDS)
        assert [(r["label"], r["start"], r["end"]) for r in runs] == [
            ("说话人1", 0.0, 1.0), ("说话人2", 1.0, 2.0), ("说话人1", 2.0, 2.5)]

    def test_scattered_flip_makes_short_run(self):
        # 散落的单窗翻转只形成 0.5s 短段 → 达不到 mixed 的 0.4s+占比门槛，
        # 不会再把"一个人的话"误拆成两个人
        eA = [0.98, 0.02]
        eB = [0.02, 0.98]
        vecs = [(0.0, 0.5, eA), (0.5, 1.0, eB), (1.0, 1.5, eA), (1.5, 2.0, eA)]
        runs = _window_runs(vecs, CENTROIDS)
        b_runs = [r for r in runs if r["label"] == "说话人2"]
        assert len(b_runs) == 1 and b_runs[0]["end"] - b_runs[0]["start"] == 0.5


class TestApportionText:
    def test_snaps_to_sentence_punct(self):
        text = "はい。そうですね、分かりました。"
        # ratio 0.4 的原始位置是 6，最近句读是「。」(index 2) → 在句号后切
        assert _apportion_text(text, 0.4) == 3

    def test_snaps_to_nearest_comma(self):
        text = "はい。そうですね、分かりました。"
        # ratio 0.55 → 原始位置 9，句读「、」在 index 8 → 切在 9
        assert _apportion_text(text, 0.55) == 9

    def test_no_punct_falls_back(self):
        text = "あいうえおかきくけこ"
        assert _apportion_text(text, 0.5) == 5

    def test_short_text(self):
        assert _apportion_text("あ", 0.5) == 1
        assert _apportion_text("", 0.5) == 0
