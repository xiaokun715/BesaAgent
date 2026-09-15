"""vLLM（自建推理服务）适配。**没有 ``embedding.py``** —— 见下。

**目录里少一个文件，本身就是一条需求**：

    vLLM 部署的通常是 chat 模型，向量化走不到它。
    → 所以「embedding 必须是可协商的**选配能力**」，
      而不是「所有 provider 都必须实现 embed()，不支持就抛 NotImplementedError」。

这条推导的落点是 ``base.Provider.embedding_model()`` 的默认实现：
它抛 :class:`provider.errors.CapabilityNotSupportedError`，
让「走错路」变成**立刻暴露的编程错误**，而不是一个返回 ``None`` 的、需要在错误位置检查的返回值。

**另一个特点：能力默认取最小集**（只有 ``chat`` + ``stream``）。
自建服务的能力取决于部署时加载了什么模型、开了哪些启动参数，
编译期不可能知道 —— 详见 ``vllm/llm.py``。
"""
