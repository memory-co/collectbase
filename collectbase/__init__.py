"""collectbase — 分层记录文件系统,层即 git 分支。

给智能体一块它够不着的地板:事实放最底层,只读;推论放上层,随便改。
层与层之间路径不相交,所以没有覆盖、没有遮蔽、没有冲突。

对外接口是 git —— `git checkout` / `git commit` / `git log`。这个包提供的
是让 git 表现出分层语义的那套东西:分支拓扑 + hook。

**库是产品,hook 只是入口之一。** server 走 plumbing,一个 hook 都不触发,
所以规则不能只活在 hook 脚本里;两份实现迟早不一致,而不一致的表现是仓库
拒绝一切提交。

设计见 docs/v2/。
"""

from .gitrepo import Git
from .layers import Layers, read as read_layers

__all__ = ["Git", "Layers", "read_layers", "__version__"]
__version__ = "0.2.0"
