"""WS/長時間HTTP転送の期限付き認可モデル。ソケット・タイマー・実転送は作成しない。"""

from editor_gateway.contracts import AccessDenied


class ConnectionGuard:
    """転送許可の期限を監視する状態機械。失効・再認可失敗で閉じ、一度閉じたら再開しない。

    実relayは独立watchdogでdeadlineにtickし、認可RPCの待機中もタイマーを止めない契約。
    本クラスだけではOS上の切断時刻は保証できない。テストは仮想時計でその契約を検証する。
    """

    def __init__(self, gateway, session, context, *, csrf=None, websocket=True):
        """開始時にも共通認可を行う。session/CSRFはreprやログに出さずメモリ内だけに置く。"""
        self.gateway, self._session, self.context = gateway, session, context
        self._csrf, self.websocket = csrf, websocket
        self.closed = False
        self.grant = gateway.authorize(session, context, csrf=csrf, websocket=websocket)
        self.last_ms = gateway._now()

    def tick(self):
        """期限到来・時計逆行を検出して閉じる。通信や認可照会で許可を自動延長しない。"""
        try:
            now = self.gateway._now()
            if now < self.last_ms or now >= self.grant.deadline_ms:
                self.closed = True
            self.last_ms = now
        except Exception:
            self.closed = True
        return not self.closed

    def permit_forward(self):
        """各方向のデータ転送直前に呼ぶ。期限後のフレーム/チャンクは一つも許可しない。"""
        if not self.tick():
            raise AccessDenied()
        return self.grant

    def reauthorize(self):
        """期限前に現在権限を再照会する。照会遅延で旧期限を超えた場合も閉じて拒否する。"""
        try:
            self.permit_forward()
            new_grant = self.gateway.authorize(self._session, self.context, csrf=self._csrf, websocket=self.websocket)
            # 照会中に旧許可が切れたら、新しい許可で遡って転送を正当化しない。
            self.permit_forward()
            self.grant = new_grant
            return new_grant
        except Exception:
            self.closed = True
            raise AccessDenied() from None
