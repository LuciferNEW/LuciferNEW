# SF2 Admin — 全功能設定工具

一支本機工具,兩大區塊。執行:`py sf2_admin.py`(自動開 http://127.0.0.1:8765,只綁本機)。

## A. 資料庫(save.db)— 不用裝任何東西
改 SFOffline 的帳號資料庫,schema / 密碼(PBKDF2)/ 道具格式都和伺服器一致。
- 上方輸入 save.db 路徑按 **Open / Create**(檔案不存在會自動建空庫,可先養好帳號再開服)。
- **Account**:改 cash / sp / tp / 等級 / codename / 擊殺數…
- **左下建帳號**:login+password → 建立(密碼算法與 server 相同,建完直接能登),可勾 seed 給新手道具。
- **Inventory**:發道具(active 單件 / inactive 堆疊 / 到期天數)。
- **道具查詢**:搜尋全部 4649 件,點一下帶入 ItemId。

## B. 伺服器內容(商店 / 扭蛋 / 新手道具)— 需要重打包
這些資料內嵌在 `SFOffline.dll` 裡,改完要**重打包**成新的 `SFOffline.exe` 才生效。
**全部是伺服器端 → 玩家不用下載任何東西,連上就看到新商店。**
- **商店**:逐件改 SP / Cash / TP / Resell 價格、上架 / 可賣;也能**新增道具**(從既有道具複製範本)。
- **封包 / 扭蛋**:編輯每個封包/扭蛋道具(pkg_itemid)開啟後給的內容物清單。
  ⚠ 本伺服器**沒有機率抽獎**:開封包會給清單裡「全部」內容物(固定內容,不是隨機抽一個)。
  要做真正「隨機機率扭蛋」需要改伺服器程式(新功能,不是改設定)——要的話再跟我說。
- **新手道具**:每個新帳號自動取得的清單。
- **產生 SFOffline.exe**:設定路徑後按「重打包 Build」。
  - 需要先一次性 `pip install pefile dnfile`。
  - **永遠從乾淨的原始 SFOffline.exe 重打包**(工具預設來源 `D:\sf\SFOffline.exe`,輸出 `D:\sf\SFOffline.patched.exe`)。
  - 完成後用新 exe `--server-only` 開服即可。

### 「新增道具」的界線
商店/價格/封包/新手全是伺服器端,玩家自動看到。但**道具本體(模型/類別 SFCItem_xxx)必須 client 已經有**。
4649 件都是台服原有的,client 都有 → 可自由上架/改價/塞給玩家。真正「全新的槍」需要 client 端美術資源(另一回事)。

## 檔案
sf2_admin.py(後端)、admin_ui.html(介面)、content.py、repack_sfoffline.py、catalogue.json / seed.json、
resources/(可編輯的 SFGameItem.eur 等,Build 的來源)、admin_config.json(路徑設定)。
