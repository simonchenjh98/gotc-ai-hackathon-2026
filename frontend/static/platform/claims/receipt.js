'use strict';

// Receipt storage is scoped to this tab and deployment path. Codes stay in memory.
function makeClaimReceiptStore(storage,namespace){
  const key='gotc-claim-receipt:v1:'+encodeURIComponent(namespace);
  const valid=value=>value && /^[a-f0-9]{32}$/.test(value.requestId) && /^[A-Za-z0-9_-]{43}$/.test(value.secret) && /^\d{6}$/.test(value.pin) && Number.isSafeInteger(value.expiresAt);
  return {
    read(){try{const raw=storage().getItem(key);if(!raw)return {receipt:null,error:''};const receipt=JSON.parse(raw);if(!valid(receipt))throw new Error();return {receipt,error:''};}catch{return {receipt:null,error:'无法读取本标签页的领取凭据，请核对浏览器存储设置。'};}},
    write(receipt){try{if(!valid(receipt))throw new Error();storage().setItem(key,JSON.stringify({requestId:receipt.requestId,secret:receipt.secret,pin:receipt.pin,expiresAt:receipt.expiresAt}));return '';}catch{return '无法保存领取凭据；刷新或关闭页面后可能无法继续领取，请保持本页并联系组织方。';}},
    clear(){try{storage().removeItem(key);return '';}catch{return '无法确认领取凭据已清除；在共享设备上请清除此站点的浏览器数据。';}}
  };
}

function claimReceiptStore(){return makeClaimReceiptStore(()=>sessionStorage,base.pathname);}
let currentClaimController=null;
function hasClaimReceipt(){return !!claimReceiptStore().read().receipt;}
function clearClaimReceipt(){currentClaimController?.clear();return claimReceiptStore().clear();}
