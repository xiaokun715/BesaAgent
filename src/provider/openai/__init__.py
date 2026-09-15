"""**OpenAI 兼容形状的基准实现**，供 ``dashscope/`` 与 ``vllm/`` 继承复用。

**这里有一个对《架构概要设计-provider》§1.2 依赖规则的正式修订**，必须先说清楚。

原文写的是「厂商层禁止依赖其它厂商子包」。按字面执行，``vllm/`` 若要复用
``HttpClient``（连接池、SSE 拆行、网络层重试、错误归一化）就得抄一份，
``dashscope/`` 走兼容模式时同理 —— 于是同一套重试与错误整形会存在三份，
然后**慢慢跑偏**，而这三份跑偏正是需求说明书 FR-P-02 / FR-P-09 / FR-P-11
要求「只有一份实现」要避免的东西。

修订后的规则：

    **允许**跨厂商复用 ``client.py`` 与「已确认为 OpenAI 兼容形状」的 ``llm.py`` / ``embedding.py``；
    **禁止**跨厂商复用 ``provider.py``，也禁止复用那些承载厂商**专有**语义的实现。

判据是「**这段代码里有没有厂商语义**」：

- ``HttpClient`` 里没有 —— HTTP 就是 HTTP，``/chat/completions`` 是事实标准而非某家私有；
- ``OpenAIChatModel`` 的四个钩子里没有 —— 它们实现的正是「OpenAI 兼容」这一**公开约定**；
- ``OpenAIProvider`` 里有 —— ``REQUIRES_API_KEY`` / ``API_KEY_ENV`` / 能力集都是**该厂商的事实**，
  所以每家必须自己写一份（哪怕只有十几行）。

被否决的方案：把 ``HttpClient`` 提到 ``provider/base.py``。否决理由是 ``base.py``
必须保持「不 import ``httpx``」这条性质（架构概要设计-provider §9 D-E），
否则契约层就绑死在某个 HTTP 库上，且无法在无网络环境被纯声明式地断言。

**继承链一览**：

    openai/client.py    HttpClient              ← 传输基准
    openai/llm.py       OpenAIChatModel         ← 四钩子的基准实现
    openai/embedding.py OpenAIEmbeddingModel
        ↑
        ├── dashscope/{client,llm,embedding}.py   走兼容模式，差异极小
        └── vllm/{client,llm}.py                  关掉鉴权、能力从配置读

每个 ``provider.py`` 都是独立写的，不复用。
"""
