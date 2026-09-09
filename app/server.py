"""Flask 后端兼容入口：路由实现已拆分到 app/web/（Blueprint），此文件仅作转发。

- create_app: 组装 Flask 应用（见 app.web.create_app）
- 路由清单 / 历史实现见 git 记录（原单文件 app/server.py）
"""
from app.web import create_app

__all__ = ["create_app"]
