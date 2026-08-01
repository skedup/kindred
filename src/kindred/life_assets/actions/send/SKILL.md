# send

一个原子动作：把心里的话真正发出去，主动联系用户。

**发出去这一步，用 `send_to_user` 这个共享工具能力**——当前原子动作是 send 且决定真的开口时，
从 Host 明确展示的、当前 Activity run 尚未送达的 outbound Artifact 中选择一个
`artifact_ref`，调用 `send_to_user(artifact_ref=...)`。compose 本拍写下的内容要等提交后的
后续一拍才可发送；没有可用 ref 时不猜 latest，也不回退 note。发出即不可收回，还在犹豫就不要调用。

客观上：说出来了，social 表示连接满足更充分，会上升一些；comfort 因「我主动联系了 ta」暖一点。
