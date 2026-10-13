function num(v){v=(v||'').toString().replace(/[٠-٩]/g,d=>'٠١٢٣٤٥٦٧٨٩'.indexOf(d)).replace(/[,٬،\s]/g,'').replace('٫','.');var n=parseFloat(v);return isNaN(n)?0:n}
function fmt(n){return n.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}
function recalc(){var d=0,c=0;document.querySelectorAll('#lines tr.line').forEach(function(r){d+=num(r.querySelector('[name=debit]').value);c+=num(r.querySelector('[name=credit]').value)});
 document.getElementById('td').textContent=fmt(d);document.getElementById('tc').textContent=fmt(c);var b=document.getElementById('bal'),ok=Math.abs(d-c)<0.005&&d>0;
 b.className='bal '+(ok?'ok':'err');b.textContent=ok?'✓ القيد متوازن':'✕ القيد غير متوازن (الفرق '+fmt(Math.abs(d-c))+')';}
function addLine(){var t=document.getElementById('rowtpl').content.cloneNode(true);document.getElementById('lines').appendChild(t);bind()}
function bind(){document.querySelectorAll('#lines input').forEach(function(i){i.oninput=recalc});document.querySelectorAll('#lines .del').forEach(function(b){b.onclick=function(){b.closest('tr').remove();recalc()}});recalc()}
document.addEventListener('DOMContentLoaded',function(){if(document.getElementById('lines')){bind()}});
