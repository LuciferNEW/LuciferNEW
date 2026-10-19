# SF2 內容編輯 + 重打包(全功能設定)

改商店/道具/槍/package(扭蛋)/新手包,然後**重打包成新的 SFOffline.exe**。這些全是 **server 端**內容,
玩家 client **不用下載更新**(client 只是顯示 server 給的結果)。

## 兩支工具
- `sf2_admin.py` — 改 **save.db**(玩家帳號、道具、錢)。見 `README.md`。
- `sf2_content.py` — 改 **遊戲內容**(商店/道具/package/新手包)並重打包。← 本說明
- `repack_sfoffline.py` — 底層重打包引擎(sf2_content 會呼叫它;也能單獨命令列用)。三個檔要放同一資料夾。

## 內容編輯器
```
cd D:\sf2_project\admin
py sf2_content.py
```
瀏覽器開 `http://127.0.0.1:8766`。上方輸入 `SFOffline.exe` 路徑(預設 `D:\sf\SFOffline.exe`)按 **Load**,
它會**直接從你的 exe 讀出內嵌資源**來編輯。

- **Shop / items / guns**:搜尋全部 4649 件;直接改 SP / Cash / TP / 上架 / 回收價,按 save;下方可**新增道具**。
- **Packages / 扭蛋**:選一個 package(pkg_itemid)→ 看/加/刪它給的子道具(sub_itemid + 數量);也能**開新 package**。
  (注意:SFOffline 的 package 是**固定內容**,開了就拿到那些東西;server 沒有機率抽獎,所以沒有「中獎率」可設。)
- **Starter kit**:新手帳號一進來就有的道具,可加/改數量/刪。

改完按右上 **Build custom SFOffline.exe** → 產生新的 exe(預設 `SFOffline_custom.exe`,放在原 exe 旁)。
拿它當 server 跑(`SFOffline_custom.exe --server-only`)就會用你的自訂內容。

## 命令列(進階)
```
py repack_sfoffline.py extract  D:\sf\SFOffline.exe  resdir      # 把 4 個資源檔抽出來手改
py repack_sfoffline.py repack   D:\sf\SFOffline.exe  resdir  SFOffline_custom.exe
py repack_sfoffline.py selftest D:\sf\SFOffline.exe              # 自我測試(改→重打包→驗證)
```

## 原理與安全性
`SFOffline.exe` 是 .NET single-file bundle;內容在內嵌的 `SFGameItem.eur`/`SFPackageItem.eur`/`SeedItems.tsv`/
`DefaultRankPoint.ini`。重打包做法:把加大後的資源放進 DLL 新增的一個 PE section、改指 CLR resources 目錄,
再重建 bundle。`.text`/`.rsrc` 和其他 179 個檔**一位元組都不動**。沒改的道具行也**原封不動**(欄位級精準修改)。

**最後一步要你驗證**:我無法在這裡跑 Windows,所以請你產生後實際用 `SFOffline_custom.exe --server-only` 跑一次,
確認有正常啟動、你的改動有生效。結構驗證(資源讀回、metadata、bundle 完整性)我都在容器測過了。
