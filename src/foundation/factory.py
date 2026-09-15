"""各能力 factory 的共同骨架：配置组 → 按 ``type`` / ``alias`` 选实现。

**形态**：把重复的四步收在一处，各能力只给 ``GROUP`` 与 ``build`` 两个东西::

    class XFactory(ModelFactory):
        GROUP = "llm"                       # 对应 settings 里的配置组名

        @classmethod
        def build(cls, klass, cfg, runtime):   # 仅当构造签名特殊时才覆写
            ...

四步是：**取配置组 → 定名 → 投影厂商默认值 → 查表构造**。

**为什么需要**：``src/provider`` 与 ``src/gateway`` 都要「按配置选实现」——
provider 按 ``type`` 选厂商适配器（openai / dashscope / vllm / mock），
gateway 按 alias 选候选链。两处各写一遍查表逻辑，就会在两处各长出一点点不同的行为：
默认值处理不一样、缓存键不一样、报错信息不一样，而且是**慢慢**长歪的，不是一次长歪。

**为什么装配点不在这里**：注册表（谁注册了哪些实现）属于**组合根**。
放在 foundation 会让底座依赖 ``src/provider``，直接违反 ``foundation/__init__.py``
声明的依赖方向。所以本模块只给**骨架与查找语义**，注册动作由各模块自己完成：
``src/provider`` 各厂商在 ``provider.py`` 里自注册，``src/gateway/registry.py``
持有 alias → 候选的解引用。

**与配置的对应关系**：改配置里的 ``type`` 就是在换实现，**业务代码一行都不用改** ——
这是《重构文件结构设计》§2.2 那条判据（「换一个实现要不要改业务代码」）在装配侧的落地。
"""
