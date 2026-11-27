# 部分再実行の履歴判定：実GitHub開始前の審査

**合成検証の範囲で準備可。実GitHubは未承認のため NO-GO。** 最新HEADのレビューと所有者による明示承認を待つ。Actionsの月額予算上限20ドルへの変更は、試験開始や課金の許可ではない。

## 何を照合するか

- 復旧系の試行ごとに、想定ジョブ集合を初回全ジョブ、A単独、B単独、C Deploy単独、C PrepareとC Deploy、全ジョブと固定。取得した現試行・過去試行のrun・attempt・PR HEAD・完全な名前集合・ジョブID・状態が計画と整合したときだけ、予定外ジョブの不在を許容する。一覧のページング、件数や集合不一致、重複、異なるrun/attempt、証拠不足で停止する。
- A2/B3は初回Validate、C4は初回C Prepareを参照する。予定された生成試行、生成ジョブのIDと成功ステップ、同一run・PR HEAD、同名の一意性、artifact ID・期待SHA-256・メタデータ、明示IDで取得した原ZIPのSHA-256を照合する。RESTのartifact自体に生成ジョブIDがあるとは仮定せず、固定した生成規約・受継ぎ出力・試行ジョブ記録を組み合わせる。
- 過去の模擬公開の成功は再送を許可する証拠としては使わない。最初に公開したattempt以後の同方式は停止する。試行1は故障注入ステップの失敗と模擬公開のスキップを要求し、後のスキップは成功済みで停止した証拠を要求する。模擬公開ステップの記録が不明、矛盾、欠落の場合も停止する。
- GitHub公式の[特定ジョブの再実行](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs)、[試行別run](https://docs.github.com/en/rest/actions/workflow-runs#get-a-workflow-run-attempt)、[試行別jobs](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run-attempt)、[artifact](https://docs.github.com/en/rest/actions/artifacts#get-an-artifact)を境界とする。[固定download-artifactの契約](https://github.com/actions/download-artifact/blob/9000827ccba6bdab643e8b6fd33ac0654aef8333/action.yml)では `artifact-ids`、`skip-decompress`、`digest-mismatch: error` を確認。ジョブ単独再実行の実際の試行別レスポンス、producer jobの出力継承、RESTと内部一覧の一時差、圧縮済みartifact取得の実バイトはこのLabでは UNKNOWN。ジョブ行に `run_attempt` がある場合は一致必須。フィールドがない場合も試行別runの `run_attempt` とそれに対応するjobs endpointは一致必須。

## 合成原票と停止条件

`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s experiments -p test_history.py -v`。API応答はすべてfixture。GitHubもPagesも接続しない。通常の旧負対照は `python3 experiments/artifacts.py negative`。

実GitHubに進む場合は、対象HEAD・Draft・固定workflow・read-only権限・Pages未使用を再確認後、所有者が承認した最大2 Run・40ランナー分・各Run 30分を超えない範囲とする。初回に仮定と一致しない観測、必須記録やハッシュの不一致、予算/時間上限に達したら、再試行せず原記録を残す。対象は模擬公開のみで、本番の公開や無断マージは含まない。
