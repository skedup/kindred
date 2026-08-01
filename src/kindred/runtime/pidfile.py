"""daemon pid 文件 + 进程锁（docs/16 §2.3）。

进程态介质：daemon 启动时获取一个**文件锁（fcntl.flock）** + 写 pid，
生命周期内持有 fd；退出（或进程死）时锁自动释放。``kindred stop`` 用
「能否拿到锁」判定 owner，避免 PID reuse 误杀。

为什么用 flock 而非裸 read→check→write（codex review N-1/N-2）：
- **N-1 原子防重启**：``flock(LOCK_EX|LOCK_NB)`` 是内核级原子操作，两个 ``kindred run``
  并发时只有一个能拿到锁，杜绝 read-check-write 的 TOCTOU 竞态。
- **N-2 owner 校验防误杀**：锁随持有进程存活而存在、进程死则内核自动释放。``stop``
  若能拿到锁 → 说明没有活的持有者（pid 文件是 stale 残留，哪怕 OS 已把该 pid
  复用给无关进程）→ 清理 + 报错，**绝不发 SIGTERM**；拿不到锁 → 确有活 daemon
  持有 → 才发信号。pid_is_alive 只能证明「pid 存在」，flock 能证明「是我们的 daemon」。

pid 文件是「重启即重生」的进程态——不随搬家，丢了无所谓（下次启动重建）。
"""

from __future__ import annotations

import errno
import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TextIO


class PidFileError(RuntimeError):
    """pid 文件操作失败（已有存活进程 / 写入失败 / stop 找不到进程）。"""


class AlreadyRunningError(PidFileError):
    """已有存活的 daemon 进程持有锁（保守拒绝，不强杀）。"""

    def __init__(self, pid: int | None, pid_file: Path) -> None:
        self.pid = pid
        self.pid_file = pid_file
        shown = pid if pid is not None else "?"
        super().__init__(
            f"daemon 已在运行（pid={shown}，pid_file={pid_file}）。"
            f"用 `kindred stop` 停止，或确认僵死后手动删除 {pid_file}。"
        )


class PidLease:
    """daemon 持有的进程租约：一个持锁 fd + pid 文件。

    daemon 生命周期内持有本对象（及其 fd）；``release`` 或进程退出时锁释放、
    pid 文件清理。**不要在 acquire 后关闭 fd**——关 fd 会丢锁。
    """

    def __init__(self, fd: TextIO, pid_file: Path) -> None:
        self._fd = fd
        self._pid_file = pid_file

    @property
    def pid_file(self) -> Path:
        return self._pid_file

    def release(self) -> None:
        """释放锁（unlock + close）。幂等，失败静默。

        **不 自行 unlink pid 文件**（codex N-4 split-inode 竞态）：若先解锁再按路径
        unlink，解锁到 unlink 的窗口里 B 已能 acquire 同路径拿锁；A 的 unlink 会删掉
        B 持锁的 inode，C 再启动新建文件拿新 inode 的锁 → B/C 同时跑。所以正常退出
        只释锁，pid 文件保留（owner 以锁为准，残留无害）；下次 acquire 在同路径 lock 后
        覆盖 pid，stale 清理交给 stop()。
        """
        if not self._fd.closed:
            try:
                fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
            except (OSError, ValueError):
                pass
            try:
                self._fd.close()
            except OSError:
                pass


def pid_is_alive(pid: int) -> bool:
    """进程是否存活。``os.kill(pid, 0)`` 不发信号只探测。

    - ESRCH → 不存在（False）
    - EPERM → 存在但无权限发信号（仍算存活 True）
    - pid<=0 → 视为无效（False，避免 kill(0/-1) 的进程组语义）

    注意：仅用于「展示 / 辅助判断」，**owner 真相以 flock 为准**（见模块 docstring）。
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return True
        raise
    return True


def _proc_starttime(pid: int) -> int | None:
    """进程启动时刻 owner token——pid + starttime 唯一标识一个进程实例，唤醒后不变。

    PID reuse 的新进程 starttime 必不同（macOS 上同 pid 同秒复用视为不可能）。
    值是**不透明 token**：acquire 记录、stop 相等比较，不跨平台比较、不做时间运算
    （Linux 是 clock ticks since boot，macOS 是 epoch 秒——单位不同无妨，只比相等）。

    - Linux：``/proc/<pid>/stat`` 第 22 字段（clock ticks）
    - macOS：``ps -o lstart=`` 启动时刻解析成 epoch 秒（见 ``_darwin_starttime``）
    - 其他平台 / 读不到 → None（调用方 fail-closed，见 ``stop``）
    """
    token = _linux_starttime(pid)
    if token is not None:
        return token
    if sys.platform == "darwin":
        return _darwin_starttime(pid)
    return None


def _linux_starttime(pid: int) -> int | None:
    """Linux ``/proc/<pid>/stat`` 第 22 字段。非 Linux / 读不到 → None。

    解析从最后一个 ')' 后切（comm 可含空格/括号）。
    """
    try:
        data = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        rparen = data.rindex(")")
        # comm 是字段 2；其后 fields[0]=state(字段 3)。starttime=字段 22 → fields[22-3]=fields[19]。
        fields = data[rparen + 2 :].split()
        return int(fields[19])
    except (ValueError, IndexError):
        return None


# `ps -o lstart=` 在 LC_ALL=C 下的固定格式（如 "Wed Jul  2 14:00:00 2026"）。
_DARWIN_LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"


def _darwin_starttime(pid: int) -> int | None:
    """macOS 进程启动时刻（epoch 秒）——``ps -p <pid> -o lstart=`` 解析。

    lstart 秒级分辨率、同一进程重复读取稳定；进程不存在时 ps 非零退出 → None。
    LC_ALL=C 钉死输出格式，避免 locale 影响解析。
    """
    try:
        # 固定参数列表（无 shell、无用户输入拼接）；ps 是 POSIX 基础工具，macOS 必有。
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return _parse_lstart(out.stdout)


def _parse_lstart(text: str) -> int | None:
    """把 lstart 文本解析成 epoch 秒；空 / 格式不符 → None。

    mktime 按本地时区换算——lstart 本身就是本地时刻，且同一字符串两次解析结果
    确定相同（token 只比相等，DST 歧义时刻也不影响判定）。
    """
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return int(time.mktime(time.strptime(stripped, _DARWIN_LSTART_FORMAT)))
    except (ValueError, OverflowError):
        return None


def read_pid(pid_file: Path) -> int | None:
    """读 pid 文件首行返回 pid；不存在 / 首行非法 → None。向后兼容只有 pid 的旧文件。"""
    pid, _ = read_owner(pid_file)
    return pid


def read_owner(pid_file: Path) -> tuple[int | None, int | None]:
    """读 (pid, starttime)。首行=pid，次行=starttime（可缺）。任一非法/缺失 → 该项 None。"""
    try:
        text = pid_file.read_text(encoding="utf-8")
    except OSError:
        return None, None
    lines = text.splitlines()
    pid: int | None = None
    starttime: int | None = None
    if lines:
        try:
            pid = int(lines[0].strip())
        except ValueError:
            pid = None
    if len(lines) >= 2:
        try:
            starttime = int(lines[1].strip())
        except ValueError:
            starttime = None
    return pid, starttime


def acquire(pid_file: Path) -> PidLease:
    """获取进程租约：原子拿文件锁 + 写 pid。返回 ``PidLease``（须持有至退出）。

    - 锁被别的活进程持有（``flock LOCK_NB`` EAGAIN/EACCES）→ ``AlreadyRunningError``
      （保守拒绝，不强杀；覆盖 N-1 并发竞态：内核保证只有一个拿到锁）。
    - 拿到锁 → 写自己的 pid（覆盖任何 stale 残留内容）。
    - 打开 / 写入失败 → ``PidFileError``。

    **不关闭 fd**——fd 持有锁，关闭即丢锁。fd 交给返回的 PidLease 管理。
    """
    try:
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        # r+ 若不存在会失败，故用 a+（可创建、可读、可定位重写）。
        fd = open(pid_file, "a+", encoding="utf-8")  # noqa: SIM115 (fd 交 PidLease 管理)
    except OSError as exc:
        raise PidFileError(f"打开 pid 文件失败：{pid_file}（{exc}）") from exc

    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        fd.close()
        if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            raise AlreadyRunningError(read_pid(pid_file), pid_file) from exc
        raise PidFileError(f"获取 pid 锁失败：{pid_file}（{exc}）") from exc

    try:
        fd.seek(0)
        fd.truncate()
        # 写 pid + starttime（owner token，N-6）：starttime 读不到（非 Linux）则留空行。
        my_pid = os.getpid()
        my_start = _proc_starttime(my_pid)
        fd.write(f"{my_pid}\n{'' if my_start is None else my_start}\n")
        fd.flush()
        os.fsync(fd.fileno())
    except OSError as exc:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        finally:
            fd.close()
        raise PidFileError(f"写 pid 文件失败：{pid_file}（{exc}）") from exc

    return PidLease(fd, pid_file)


class OwnerNotReadyError(PidFileError):
    """有活持锁者但 owner 未就绪（daemon 正在启动 / pid 未写入 / starttime 不匹配），稍后重试。"""


def stop(pid_file: Path) -> int:
    """优雅停止活 daemon（发 SIGTERM），返回被通知的 pid。

    **probe-first + owner metadata**（codex N-6）——不在锁判定前信任 pid 内容：
    1. 先 open + 探锁（**不预读 pid**）。
    2. **能拿到锁** → 无活持有者（stale，含 PID reuse）→ ``PidFileError``（未运行），
       **不发信号、不 unlink**（N-5）。
    3. **拿不到锁** = 有活持锁者 → 读 (pid, starttime) 验证 owner：
       - pid 未就绪 / 进程已死 / 本机读不到 starttime / starttime 与 ``/proc/<pid>/stat`` 不匹配
         → ``OwnerNotReadyError``（daemon 正在启动的“拿锁→写 pid”窗口里读到旧 stale pid，
         PID reuse，或非 Linux 平台 owner 不可证明），**不发信号**。
       - **仅当 starttime 匹配（owner 已证明）→ 发 SIGTERM**。owner 证不了就不动手，
         贯彻所有平台（N-8）：不因「平台读不到 starttime」而降级回 pid-only（那会重开误杀窗口）。

    为什么 owner metadata：``acquire`` 先 flock 后写 pid，中间窗口里 pid 文件仍是上一轮
    stale 内容；仅凭“拿不到锁”就对读到的 pid 发信号，会误杀 PID reuse 后的无关进程。
    pid+starttime 唯一标识进程实例，能证明“该 pid 就是当初写入的那个进程”。
    """
    try:
        probe = open(pid_file, "a+", encoding="utf-8")  # noqa: SIM115 (下方显式关闭)
    except OSError as exc:
        raise PidFileError(f"打开 pid 文件失败：{pid_file}（{exc}）") from exc

    try:
        try:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise PidFileError(f"探测 pid 锁失败：{pid_file}（{exc}）") from exc
        else:
            # 拿到锁 = 无活持有者（stale，含 PID reuse）→ 未运行，不发信号、不 unlink（N-5）。
            try:
                fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            raise PidFileError(
                f"daemon 未在运行（pid_file stale，无活持锁者；残留由下次启动持锁覆盖：{pid_file}）"
            )
    finally:
        probe.close()

    # 拿不到锁 = 有活持锁者。此时才读 pid+starttime 做 owner 校验（N-6）。
    pid, recorded_start = read_owner(pid_file)
    if pid is None:
        raise OwnerNotReadyError(
            f"daemon 正在启动（已持锁但 pid 未就绪），稍后重试：{pid_file}"
        )
    if not pid_is_alive(pid):
        raise OwnerNotReadyError(
            f"pid_file 记录的 pid={pid} 已死但锁仍被持有（owner 未就绪），稍后重试：{pid_file}"
        )
    # owner 证明贯彻所有平台（N-8）：**证不了 owner 就不发信号**，绝不因「平台读不到
    # starttime」而降级回 pid-only（那会重新打开 PID reuse 误杀窗口）。
    actual_start = _proc_starttime(pid)
    if actual_start is None:
        # 读不到进程 starttime（Linux /proc、macOS ps 都不可用）→ 无法证明 owner → fail-closed。
        # 其他平台的可验证 owner token 方案（如 psutil.create_time）引依待定，在此之前不赌误杀。
        raise OwnerNotReadyError(
            f"本平台读不到进程 starttime，无法证明 pid={pid} 是持锁者，拒发信号（fail-closed）；"
            f"Kindred stop 目前支持 Linux / macOS：{pid_file}"
        )
    if recorded_start is None:
        # 旧的一行 pidfile 无 starttime：迁移/残留窗口里无法证明读到的 pid 就是持锁者。
        # 若该 pid 已被复用给无关存活进程，发信号会误杀 → 拒发（N-7）。
        raise OwnerNotReadyError(
            f"pid_file 缺失 starttime（旧格式/残留）无法证明 pid={pid} 是持锁者，拒发信号；"
            f"请确认 daemon 状态后重试：{pid_file}"
        )
    if recorded_start != actual_start:
        # starttime 不匹配 = 读到的 pid 已被 PID reuse（或启动窗口旧内容）→ 不误杀。
        raise OwnerNotReadyError(
            f"pid={pid} 的 starttime 不匹配（记录={recorded_start} 实际={actual_start}），"
            f"该 pid 可能已被复用，拒发信号：{pid_file}"
        )
    # 至此 owner 已证明：pid 存活 + starttime 匹配 → 发 SIGTERM。
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as kill_exc:
        raise PidFileError(f"发送 SIGTERM 给 pid={pid} 失败：{kill_exc}") from kill_exc
    return pid
