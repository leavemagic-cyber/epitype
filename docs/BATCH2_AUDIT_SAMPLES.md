# J：送達後違反的五例核對

2026-10-01 逐列讀取兩本原帳。送達帳：治理庫 .epitype/recall_deliveries.jsonl；閘帳：治理庫 _GATE_LOG.jsonl。五例共同場次以本機證據的樣本 C 指向原檔，私有場次編號不進公開 repo；時間皆 UTC。卡名與 decision_key／name／stem 對照，同場且送達早於擋下才成立。

|規則|送達行／時間（09-29）|閘帳行／時間|指紋|核對|
|---|---|---|---|---|
|completion-carries-evidence|248／01:38:07|1254／03:21:13|494f6c7c779154a1|require，成立|
|feedback_review_model_by_difficulty|327／04:02:05|1258／04:05:10|e3924ad81e64429a|require，成立|
|state-losses-before-stopping|416／05:14:34|1262／05:42:46|b62c1130363e9a59|require，成立|
|delegated-work-keeps-its-owner|315／04:02:04|1269／07:29:03|f16d74e894760fe3|turn_check，成立|
|correction-carries-a-recheck|273／02:14:01|1284／13:13:23|58465aa9b985d94b|require，成立|

第二例送達列的 vault 明確是 daipai 庫；送達帳雖集中存在治理庫，仍須依列內 vault 組成卡片身分，不能誤認成治理庫同名卡。現行 compliance.deliveries 已使用列內 vault。

這五例證明報表的「送達之後仍命中閘」關係；不證明模型曾讀懂規則，也不證明該閘的字面條件就是正確的語意判斷。未將五例外推成整張報表完全正確。
