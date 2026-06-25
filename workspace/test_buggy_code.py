from buggy_code import add


def test_add():
    # 初始代码里 add(1, 2) 会返回 -1，所以第一次 pytest 会失败。
    # Agent 修复为加法后，这个测试会通过。
    assert add(1, 2) == 3
