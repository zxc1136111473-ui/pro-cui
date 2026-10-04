"""deploy.sh 的铺入 / 同步逻辑：从真实的 deploy.sh 里抽出函数，放进临时目录的沙箱里跑（不碰 docker）。
覆盖：自带节点（pro-* 同步并备份旧版、relayapi 不覆盖只提示）、内置工作流（有差异只提示、--refresh-workflows 先备份再覆盖）、
--sync（节点有变化才问「有任务在跑」并重启）。同时用系统 bash（macOS 是 3.2）和 Homebrew bash 5 各跑一遍，脚本不能依赖新语法。"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
with open(os.path.join(ROOT, "deploy.sh"), encoding="utf-8") as f:
    SRC = f.read()


def fn(name):
    m = re.search(r"^%s\(\) \{\n.*?^\}\n" % re.escape(name), SRC, re.M | re.S)
    assert m, f"deploy.sh 里没找到函数 {name}"
    return m.group(0)


FUNCS = "\n".join(fn(n) for n in ("repo_files_differ", "scan_custom_nodes", "seed_custom_nodes", "seed_assets", "seed_workflows", "do_refresh_workflows", "do_sync"))
HEAD = '''set -euo pipefail
say() { printf '%s\\n' "$*"; }
ok() { printf 'OK %s\\n' "$*"; }
warn() { printf 'WARN %s\\n' "$*"; }
die() { printf 'DIE %s\\n' "$*"; exit 3; }
self_cmd() { printf 'bash deploy.sh'; }
json_out() { :; }
NODES_NEW=(); NODES_PRO=(); NODES_OTHER=(); NODES_OTHER_FILES=()
'''

BASHES = [b for b in ("/bin/bash", "/opt/homebrew/bin/bash", "/usr/bin/bash") if os.path.exists(b)]
BASHES = [b for i, b in enumerate(BASHES) if os.path.realpath(b) not in [os.path.realpath(x) for x in BASHES[:i]]]


class Sandbox:
    def __init__(self):
        self.dir = tempfile.mkdtemp()
        self.app = os.path.join(self.dir, "app")
        for d in ("custom-nodes", "workflows", "assets", "data/custom_nodes", "data/user/default/workflows", "data/input"):
            os.makedirs(os.path.join(self.app, d))

    def w(self, rel, text="x"):
        p = os.path.join(self.app, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def r(self, rel):
        with open(os.path.join(self.app, rel), encoding="utf-8") as f:
            return f.read()

    def exists(self, rel):
        return os.path.exists(os.path.join(self.app, rel))

    def ls(self, rel):
        p = os.path.join(self.app, rel)
        return sorted(os.listdir(p)) if os.path.isdir(p) else []

    def run(self, bash, body, stubs="", funcs=None):
        script = HEAD + 'APP_DIR="%s"\n' % self.app + stubs + (funcs or FUNCS) + "\n" + body
        # 容错解码：bash 3.2 在报错时可能把多字节中文截断，不能让解码错误盖住真正的失败原因
        p = subprocess.run([bash, "-c", script], capture_output=True)
        return p.returncode, (p.stdout + p.stderr).decode("utf-8", errors="replace")

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def each_bash(test):
    def wrapper(self):
        for b in BASHES:
            sb = Sandbox()
            try:
                test(self, sb, b)
            except AssertionError as e:
                raise AssertionError(f"[{b}] {e}") from None
            finally:
                sb.close()
    wrapper.__name__ = test.__name__
    return wrapper


class CustomNodes(unittest.TestCase):
    @each_bash
    def test_fresh_install_copies_everything(self, sb, bash):
        sb.w("custom-nodes/pro-a/__init__.py", "A"); sb.w("custom-nodes/ComfyUI-relayapi/__init__.py", "R")
        rc, out = sb.run(bash, "seed_custom_nodes")
        self.assertEqual(rc, 0, out)
        self.assertEqual(sb.ls("data/custom_nodes"), ["ComfyUI-relayapi", "pro-a"])
        self.assertIn("已铺入 2 个自带节点包", out)

    @each_bash
    def test_identical_is_silent_and_makes_no_backup(self, sb, bash):
        sb.w("custom-nodes/pro-a/__init__.py", "A"); sb.w("data/custom_nodes/pro-a/__init__.py", "A")
        sb.w("data/custom_nodes/pro-a/__pycache__/x.pyc", "cache")   # Python 缓存不算差异
        rc, out = sb.run(bash, "seed_custom_nodes")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.strip(), "")
        self.assertEqual([d for d in sb.ls("data") if "备份" in d], [])

    @each_bash
    def test_pro_pack_is_synced_exactly_and_old_version_backed_up(self, sb, bash):
        sb.w("custom-nodes/pro-a/__init__.py", "NEW"); sb.w("custom-nodes/pro-a/added.py", "added")
        sb.w("data/custom_nodes/pro-a/__init__.py", "OLD"); sb.w("data/custom_nodes/pro-a/removed_in_repo.py", "gone")
        sb.w("data/custom_nodes/pro-user-own/__init__.py", "mine")     # 仓库里没有的（用户自己装的）不碰
        rc, out = sb.run(bash, "seed_custom_nodes")
        self.assertEqual(rc, 0, out)
        self.assertEqual(sb.ls("data/custom_nodes/pro-a"), ["__init__.py", "added.py"])   # 和仓库完全一致（旧文件被清掉）
        self.assertEqual(sb.r("data/custom_nodes/pro-a/__init__.py"), "NEW")
        self.assertEqual(sb.r("data/custom_nodes/pro-user-own/__init__.py"), "mine")
        baks = [d for d in sb.ls("data") if d.startswith("custom_nodes-旧版备份-")]
        self.assertEqual(len(baks), 1)
        self.assertEqual(sb.r(f"data/{baks[0]}/pro-a/__init__.py"), "OLD")                # 旧版进了备份
        self.assertEqual(sb.r(f"data/{baks[0]}/pro-a/removed_in_repo.py"), "gone")
        self.assertIn("已把 1 个自带节点包同步成仓库版本：pro-a", out)
        self.assertFalse(any(d.startswith(".sync-") for d in sb.ls("data")), "临时目录没清掉")
        self.assertNotIn(".sync-pro-a", sb.ls("data/custom_nodes"))

    @each_bash
    def test_relayapi_is_never_overwritten_and_config_survives(self, sb, bash):
        sb.w("custom-nodes/ComfyUI-relayapi/nodes.py", "NEW")
        sb.w("data/custom_nodes/ComfyUI-relayapi/nodes.py", "OLD"); sb.w("data/custom_nodes/ComfyUI-relayapi/relay_config.json", "KEYS")
        rc, out = sb.run(bash, "seed_custom_nodes")
        self.assertEqual(rc, 0, out)
        self.assertEqual(sb.r("data/custom_nodes/ComfyUI-relayapi/nodes.py"), "OLD")
        self.assertEqual(sb.r("data/custom_nodes/ComfyUI-relayapi/relay_config.json"), "KEYS")
        self.assertIn("ComfyUI-relayapi 和仓库版本不同，没覆盖", out)
        self.assertIn("nodes.py", out)          # 提示里要说差在哪个文件
        self.assertIn("cp -R", out)

    @each_bash
    def test_relayapi_extra_files_on_the_server_are_not_a_difference(self, sb, bash):
        """服务器这边多出来的文件（旧版遗留的 .github/、本机的 relay_config.json）不算差异；只看仓库里有的文件。"""
        sb.w("custom-nodes/ComfyUI-relayapi/nodes.py", "S"); sb.w("custom-nodes/ComfyUI-relayapi/js/a.js", "J")
        sb.w("data/custom_nodes/ComfyUI-relayapi/nodes.py", "S"); sb.w("data/custom_nodes/ComfyUI-relayapi/js/a.js", "J")
        sb.w("data/custom_nodes/ComfyUI-relayapi/relay_config.json", "KEYS")
        sb.w("data/custom_nodes/ComfyUI-relayapi/.github/workflows/x.yml", "legacy")
        sb.w("data/custom_nodes/ComfyUI-relayapi/__pycache__/n.pyc", "cache")
        rc, out = sb.run(bash, "seed_custom_nodes")
        self.assertEqual((rc, out.strip()), (0, ""))

    @each_bash
    def test_relayapi_file_missing_on_the_server_is_a_difference_and_named(self, sb, bash):
        sb.w("custom-nodes/ComfyUI-relayapi/nodes.py", "S"); sb.w("custom-nodes/ComfyUI-relayapi/UPSTREAM.md", "doc")
        sb.w("data/custom_nodes/ComfyUI-relayapi/nodes.py", "S")
        rc, out = sb.run(bash, "seed_custom_nodes")
        self.assertEqual(rc, 0, out)
        self.assertIn("UPSTREAM.md", out)
        self.assertNotIn("nodes.py", out.split("UPSTREAM.md")[0].split("没覆盖")[-1])   # 一致的文件不会被列出来
        self.assertFalse(sb.exists("data/custom_nodes/ComfyUI-relayapi/UPSTREAM.md"), "只提示，不自动补")

    @each_bash
    def test_sync_still_reports_the_other_pack_without_restarting(self, sb, bash):
        """relayapi 有差异只提示：不算「节点有变化」，不问、不重启。"""
        sb.w("custom-nodes/ComfyUI-relayapi/nodes.py", "NEW"); sb.w("data/custom_nodes/ComfyUI-relayapi/nodes.py", "OLD")
        rc, out = sb.run(bash, "do_sync", Sync.STUBS)
        self.assertEqual(rc, 0, out)
        self.assertIn("没覆盖", out)
        self.assertNotIn("BUSY_ASKED", out)
        self.assertNotIn("RESTARTED", out)


class Workflows(unittest.TestCase):
    @each_bash
    def test_seed_copies_missing_and_only_warns_about_differences(self, sb, bash):
        sb.w("workflows/1-图片/01-a.json", "NEW-A"); sb.w("workflows/1-图片/02-b.json", "B"); sb.w("workflows/3-视频/10-c.json", "C")
        sb.w("data/user/default/workflows/1-图片/01-a.json", "USER-EDITED")   # 差异：不覆盖
        sb.w("data/user/default/workflows/1-图片/02-b.json", "B")             # 一致：不动
        rc, out = sb.run(bash, "seed_workflows")
        self.assertEqual(rc, 0, out)
        self.assertEqual(sb.r("data/user/default/workflows/1-图片/01-a.json"), "USER-EDITED")
        self.assertEqual(sb.r("data/user/default/workflows/3-视频/10-c.json"), "C")
        self.assertIn("已铺入 1 个内置工作流", out)
        self.assertIn("有 1 个内置工作流和仓库版本不同", out)
        self.assertIn("--refresh-workflows", out)

    @each_bash
    def test_seed_is_silent_when_everything_matches(self, sb, bash):
        sb.w("workflows/1-图片/01-a.json", "A"); sb.w("data/user/default/workflows/1-图片/01-a.json", "A")
        rc, out = sb.run(bash, "seed_workflows")
        self.assertEqual((rc, out.strip()), (0, ""))

    @each_bash
    def test_refresh_backs_up_then_overwrites_and_leaves_user_files(self, sb, bash):
        sb.w("workflows/1-图片/01-a.json", "NEW-A"); sb.w("workflows/1-图片/02-b.json", "B"); sb.w("workflows/3-视频/10-c.json", "C")
        sb.w("data/user/default/workflows/1-图片/01-a.json", "OLD-A")
        sb.w("data/user/default/workflows/1-图片/02-b.json", "B")
        sb.w("data/user/default/workflows/我自己存的.json", "MINE")                       # 不在仓库里：不碰，只列出来
        sb.w("data/user/default/workflows/1-一条龙/12-旧编号.json", "LEGACY")
        rc, out = sb.run(bash, "do_refresh_workflows")
        self.assertEqual(rc, 0, out)
        self.assertEqual(sb.r("data/user/default/workflows/1-图片/01-a.json"), "NEW-A")
        self.assertEqual(sb.r("data/user/default/workflows/3-视频/10-c.json"), "C")
        self.assertEqual(sb.r("data/user/default/workflows/我自己存的.json"), "MINE")
        self.assertEqual(sb.r("data/user/default/workflows/1-一条龙/12-旧编号.json"), "LEGACY")
        baks = [d for d in sb.ls("data/user/default") if d.startswith("workflows-旧版备份-")]
        self.assertEqual(len(baks), 1)
        self.assertEqual(sb.r(f"data/user/default/{baks[0]}/1-图片/01-a.json"), "OLD-A")
        self.assertEqual(sb.ls(f"data/user/default/{baks[0]}/1-图片"), ["01-a.json"])      # 一致的不进备份
        self.assertIn("补了 1 个，换成仓库版本 1 个", out)
        self.assertIn("还有 2 个不在仓库里", out)
        # 再跑一次：已经一致，不再产生备份
        rc2, out2 = sb.run(bash, "do_refresh_workflows")
        self.assertEqual(rc2, 0, out2)
        self.assertIn("已经和仓库一致", out2)
        self.assertEqual(len([d for d in sb.ls("data/user/default") if d.startswith("workflows-旧版备份-")]), 1)

    @each_bash
    def test_refresh_without_workflows_dir_dies(self, sb, bash):
        shutil.rmtree(os.path.join(sb.app, "workflows"))
        rc, out = sb.run(bash, "do_refresh_workflows")
        self.assertEqual(rc, 3)
        self.assertIn("DIE", out)


class Sync(unittest.TestCase):
    STUBS = '''docker_ok() { return 0; }
DOCKER=dockerstub; CONTAINER=comfyui
dockerstub() { if [ "${1:-}" = ps ] && [ -n "${HAVE_CONTAINER-abc123}" ]; then echo "${HAVE_CONTAINER-abc123}"; fi; }
busy_guard() { echo "BUSY_ASKED"; return ${BUSY_RC:-0}; }
restart_container() { echo "RESTARTED"; }
'''

    @each_bash
    def test_node_change_asks_syncs_and_restarts(self, sb, bash):
        sb.w("custom-nodes/pro-a/__init__.py", "NEW"); sb.w("data/custom_nodes/pro-a/__init__.py", "OLD")
        rc, out = sb.run(bash, "do_sync", self.STUBS)
        self.assertEqual(rc, 0, out)
        self.assertEqual(sb.r("data/custom_nodes/pro-a/__init__.py"), "NEW")
        self.assertLess(out.index("BUSY_ASKED"), out.index("同步成仓库版本"), "要先问「有任务在跑」，再动文件")
        self.assertIn("RESTARTED", out)
        self.assertIn("强制刷新", out)

    @each_bash
    def test_declined_busy_guard_touches_nothing(self, sb, bash):
        sb.w("custom-nodes/pro-a/__init__.py", "NEW"); sb.w("data/custom_nodes/pro-a/__init__.py", "OLD")
        sb.w("workflows/1-图片/01-a.json", "A")
        rc, out = sb.run(bash, "do_sync", "BUSY_RC=1\n" + self.STUBS)
        self.assertEqual(rc, 0, out)
        self.assertEqual(sb.r("data/custom_nodes/pro-a/__init__.py"), "OLD")
        self.assertFalse(sb.exists("data/user/default/workflows/1-图片/01-a.json"), "取消时工作流也不该铺入")
        self.assertNotIn("RESTARTED", out)
        self.assertIn("已取消，什么都没动", out)

    @each_bash
    def test_no_node_change_means_no_question_and_no_restart(self, sb, bash):
        sb.w("custom-nodes/pro-a/__init__.py", "A"); sb.w("data/custom_nodes/pro-a/__init__.py", "A")
        sb.w("workflows/1-图片/01-a.json", "A")                                  # 工作流新增：照样铺入，但不需要重启
        rc, out = sb.run(bash, "do_sync", self.STUBS)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("BUSY_ASKED", out)
        self.assertNotIn("RESTARTED", out)
        self.assertIn("没有变化，不用重启", out)
        self.assertEqual(sb.r("data/user/default/workflows/1-图片/01-a.json"), "A")

    @each_bash
    def test_new_pack_counts_as_a_change(self, sb, bash):
        sb.w("custom-nodes/pro-new/__init__.py", "N")
        rc, out = sb.run(bash, "do_sync", self.STUBS)
        self.assertEqual(rc, 0, out)
        self.assertTrue(sb.exists("data/custom_nodes/pro-new/__init__.py"))
        self.assertIn("RESTARTED", out)

    @each_bash
    def test_dies_when_never_deployed(self, sb, bash):
        sb.w("custom-nodes/pro-a/__init__.py", "A")
        rc, out = sb.run(bash, "do_sync", "HAVE_CONTAINER=\n" + self.STUBS)
        self.assertEqual(rc, 3)
        self.assertIn("还没部署过", out)

    @each_bash
    def test_assets_are_seeded_but_not_overwritten(self, sb, bash):
        sb.w("custom-nodes/pro-a/__init__.py", "A"); sb.w("data/custom_nodes/pro-a/__init__.py", "A")
        sb.w("assets/请上传视频.mp4", "PLACEHOLDER"); sb.w("assets/无背景音乐.wav", "NEW-WAV")
        sb.w("data/input/无背景音乐.wav", "USER-WAV")
        rc, out = sb.run(bash, "do_sync", self.STUBS)
        self.assertEqual(rc, 0, out)
        self.assertEqual(sb.r("data/input/请上传视频.mp4"), "PLACEHOLDER")
        self.assertEqual(sb.r("data/input/无背景音乐.wav"), "USER-WAV")


ACCESS_FUNCS = "\n".join(fn(n) for n in ("probe_host", "bind_scope", "do_check"))
ACCESS_STUBS = '''YLW=""; RST=""; BLD=""; CADDY_GW="172.18.0.1"; CONTAINER=comfyui
docker_ok() { return 0; }
DOCKER=dockerstub
dockerstub() { :; }
state_read() { case "$1" in bind) printf '%s' "${TEST_BIND-}" ;; port) printf '8188' ;; *) printf '%s' "${2:-}" ;; esac; }
hostname() { echo 10.0.0.5; }
plugin_list() { echo; }
'''


class AccessMessage(unittest.TestCase):
    """端口开放范围：probe_host 对「bind 为空」和「绑 127.0.0.1」都返回 127.0.0.1（它只管探活连哪），
    体检拿它判断就把「只绑本机」误报成「所有网卡开放，无鉴权」；现在用 bind_scope 判断。"""

    @each_bash
    def test_bind_scope_mapping(self, sb, bash):
        for bind, want in (("", "open"), ("0.0.0.0", "open"), ("127.0.0.1", "loopback"), ("localhost", "loopback"),
                           ("::1", "loopback"), ("172.18.0.1", "addr"), ("10.0.0.5", "addr")):
            rc, out = sb.run(bash, 'TEST_BIND="%s"; printf "[%%s]" "$(bind_scope)"' % bind, ACCESS_STUBS, ACCESS_FUNCS)
            self.assertEqual(out.strip(), f"[{want}]", f"bind={bind!r}")

    def check(self, sb, bash, bind):
        rc, out = sb.run(bash, 'TEST_BIND="%s"; do_check || true' % bind, ACCESS_STUBS, ACCESS_FUNCS)
        return out

    @each_bash
    def test_open_to_all_interfaces_is_warned_about(self, sb, bash):
        for bind in ("", "0.0.0.0"):
            out = self.check(sb, bash, bind)
            self.assertIn("所有网卡开放，无鉴权", out, f"bind={bind!r}")
            self.assertIn("http://10.0.0.5:8188/", out)

    @each_bash
    def test_loopback_only_is_not_reported_as_open(self, sb, bash):
        for bind in ("127.0.0.1", "localhost", "::1"):
            out = self.check(sb, bash, bind)
            self.assertNotIn("所有网卡开放", out, f"bind={bind!r}")
            self.assertNotIn("无鉴权", out)
            self.assertIn(f"只绑本机 {bind}:8188（外网直连不通）", out)

    @each_bash
    def test_specific_address_keeps_the_caddy_message(self, sb, bash):
        out = self.check(sb, bash, "172.18.0.1")
        self.assertNotIn("所有网卡开放", out)
        self.assertIn("只对 172.18.0.1:8188 开放 —— 走 Caddy 配的域名访问", out)

    def test_probe_host_is_not_used_to_decide_exposure_anywhere(self):
        self.assertNotIn('[ "$(probe_host)" = "127.0.0.1" ]', SRC)


class WiringInDeploySh(unittest.TestCase):
    def test_new_actions_are_wired_up(self):
        for needle in ('--sync)      ZFC_ACTION="sync"', '--refresh-workflows) ZFC_ACTION="refresh-workflows"',
                       'if [ "$ZFC_ACTION" = "sync" ]; then do_sync; exit 0; fi',
                       'if [ "$ZFC_ACTION" = "refresh-workflows" ]; then do_refresh_workflows; exit 0; fi',
                       "s|S) do_sync; exit 0 ;;", "w|W) do_refresh_workflows; exit 0 ;;"):
            self.assertIn(needle, SRC)

    def test_no_bare_variable_directly_before_a_cjk_char(self):
        """变量后面直接跟中文 / 全角标点时必须写成 ${var}：macOS 的 bash（含 5.x）会把那个字节当成变量名的一部分，
        在 set -u 下直接报 unbound variable（这个脚本一直是这么写的，测试守住这个约定）。"""
        pat = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*(?=[^\x00-\x7f])")
        bad = [f"{i}: {l.strip()[:80]}" for i, l in enumerate(SRC.split("\n"), 1) if pat.search(l)]
        self.assertEqual(bad, [], "这些行里 $变量 后面直接跟了中文，改成 ${变量}")

    def test_help_and_syntax(self):
        for b in BASHES:
            self.assertEqual(subprocess.run([b, "-n", os.path.join(ROOT, "deploy.sh")], capture_output=True).returncode, 0, b)
        out = subprocess.run(["bash", os.path.join(ROOT, "deploy.sh"), "--help"], capture_output=True, text=True).stdout
        self.assertIn("--sync", out)
        self.assertIn("--refresh-workflows", out)


if __name__ == "__main__":
    unittest.main()
