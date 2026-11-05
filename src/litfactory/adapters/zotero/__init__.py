"""Zotero 文献库：用户新加入的条目作为收件箱信号源，库里已有的 PDF 作为全文来源。

两种模式：local 直接读本机 zotero.sqlite 的只读副本；web 读 Zotero Web API v3。
只依赖 adapters.common、domain、ports，不依赖其他适配器。
"""
