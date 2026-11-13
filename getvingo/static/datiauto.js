// Dati dell'auto (ingresso in stock e Modifica dati), 13/11/2026:
// - versione da scegliere tra quelle del modello per quell'anno (le più usate prima); se non c'è, "Altra versione…"
//   e si scrive (la volta dopo sarà nell'elenco). Elenco da /api/versioni: cresce con le auto caricate
// - colore obbligatorio, con i colori già usati per la marca come suggerimenti
// - marca, modello, versione e colore si correggono uscendo dal campo con le stesse regole del salvataggio
//   (/api/formatta: "bianco perla" → "Bianco Perla", "golf gti" → "Golf GTI")
(function () {
  var form = document.querySelector('[data-dati-auto]');
  if (!form) return;
  var campo = function (n) { return form.querySelector('[name=' + n + ']'); };
  var scelta = form.querySelector('[data-versione-scelta]');
  var scritta = campo('versione');
  var aiuto = form.querySelector('[data-versione-aiuto]');
  var colori = form.querySelector('#colori-auto');
  var ALTRA = '__altra__', timer = null, ultima = '';

  function modoScrittura(si) {
    scritta.hidden = !si; scritta.required = si;
    scelta.required = !si && !scelta.hidden;
    if (si && document.activeElement === scelta) scritta.focus();
  }
  function opzione(testo, valore, gruppo) {
    var o = document.createElement('option'); o.textContent = testo; o.value = valore; (gruppo || scelta).appendChild(o); return o;
  }
  function carica() {
    var marca = campo('marca').value.trim(), modello = campo('modello').value.trim(), anno = campo('anno').value;
    var chiave = marca + '|' + modello + '|' + anno;
    if (chiave === ultima) return;
    ultima = chiave;
    if (!marca || !modello) { scelta.hidden = true; modoScrittura(true); aiuto.textContent = ''; return; }
    fetch('/api/versioni?' + new URLSearchParams({ marca: marca, modello: modello, anno: anno }))
      .then(function (r) { return r.ok ? r.json() : { anno: [], altre: [], colori: [] }; })
      .then(function (d) {
        if (colori) colori.innerHTML = (d.colori || []).map(function (c) { return '<option value="' + c.replace(/"/g, '&quot;') + '">'; }).join('');
        var attuale = scritta.value.trim();
        scelta.innerHTML = '';
        opzione('Scegli la versione…', '');
        if (d.anno.length) {
          var g1 = document.createElement('optgroup'); g1.label = 'Anno ' + anno; scelta.appendChild(g1);
          d.anno.forEach(function (v) { opzione(v, v, g1); });
        }
        if (d.altre.length) {
          var g2 = document.createElement('optgroup'); g2.label = d.anno.length || anno ? 'Altri anni' : 'Versioni di ' + modello; scelta.appendChild(g2);
          d.altre.forEach(function (v) { opzione(v, v, g2); });
        }
        opzione('Altra versione (la scrivo)…', ALTRA);
        var tutte = d.anno.concat(d.altre);
        if (!tutte.length) {
          scelta.hidden = true; modoScrittura(true);
          aiuto.textContent = 'Prima ' + marca + ' ' + modello + ' in Getvingo: scrivi la versione, la prossima volta si sceglierà dall’elenco.';
          return;
        }
        scelta.hidden = false;
        aiuto.textContent = d.anno.length ? '' : (anno ? 'Nessuna versione già usata per il ' + anno + ': scegli tra quelle degli altri anni o scrivila.' : 'Scegli prima l’anno per vedere le sue versioni.');
        var uguale = tutte.filter(function (v) { return v.toLowerCase() === attuale.toLowerCase(); })[0];
        if (uguale) { scelta.value = uguale; scritta.value = uguale; modoScrittura(false); }
        else if (attuale) { scelta.value = ALTRA; modoScrittura(true); }
        else { scelta.value = ''; modoScrittura(false); }
      }).catch(function () { scelta.hidden = true; modoScrittura(true); });
  }
  scelta.addEventListener('change', function () {
    if (scelta.value === ALTRA) { scritta.value = ''; modoScrittura(true); }
    else { scritta.value = scelta.value; modoScrittura(false); }
    form.dispatchEvent(new Event('input', { bubbles: true }));
  });
  ['marca', 'modello', 'anno'].forEach(function (n) {
    campo(n).addEventListener('change', carica);
    campo(n).addEventListener('input', function () { clearTimeout(timer); timer = setTimeout(carica, 600); });
  });
  document.addEventListener('vin-precompilato', function () { setTimeout(carica, 50); });

  // scrittura uniforme, uscendo dal campo
  ['marca', 'modello', 'versione', 'colore'].forEach(function (n) {
    var el = campo(n);
    el.addEventListener('blur', function () {
      var t = el.value.trim();
      if (!t) return;
      fetch('/api/formatta?' + new URLSearchParams({ campo: n, testo: t, marca: campo('marca').value, modello: campo('modello').value }))
        .then(function (r) { return r.json(); }).then(function (d) {
          if (d.testo && d.testo !== el.value && document.activeElement !== el) {
            el.value = d.testo;
            form.dispatchEvent(new Event('input', { bubbles: true }));
          }
        }).catch(function () {});
    });
  });
  carica();
})();
