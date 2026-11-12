// Lettura del VIN da foto: prima codice a barre (nativo o ZXing), poi OCR (Tesseract).
(function () {
  var input = document.getElementById('foto-vin');
  var campo = document.getElementById('vin');
  var stato = document.getElementById('stato-lettura');
  var conta = document.getElementById('conta-vin');
  var VIN_RE = /[A-HJ-NPR-Z0-9]{17}/;

  function normalizza(t) {
    return (t || '').toUpperCase().replace(/[\s\-\.]/g, '')
      .replace(/O/g, '0').replace(/I/g, '1').replace(/Q/g, '0');
  }
  // Cifra di controllo ISO 3779 (posizione 9): obbligatoria in Nord America, spesso presente anche in Europa.
  var VALORI = { A: 1, B: 2, C: 3, D: 4, E: 5, F: 6, G: 7, H: 8, J: 1, K: 2, L: 3, M: 4, N: 5, P: 7, R: 9,
                 S: 2, T: 3, U: 4, V: 5, W: 6, X: 7, Y: 8, Z: 9 };
  var PESI = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2];
  function controlloOk(v) {
    var somma = 0;
    for (var i = 0; i < 17; i++) somma += (/[0-9]/.test(v[i]) ? +v[i] : VALORI[v[i]]) * PESI[i];
    var resto = somma % 11;
    return v[8] === (resto === 10 ? 'X' : String(resto));
  }
  function estrai(testo) {
    // L'OCR legge anche ciò che sta intorno ("VIN", "FIN/VIN:", "Telaio"): prima si tolgono le etichette,
    // poi si valutano tutti i candidati da 17 caratteri e si sceglie il più credibile.
    var migliore = null;
    function proponi(vin, punti) {
      if (!VIN_RE.test(vin) || vin.length !== 17) return;
      if (controlloOk(vin)) punti += 3;
      if (/^[A-Z]/.test(vin)) punti += 1;           // il costruttore (WMI) inizia quasi sempre con una lettera
      if (!migliore || punti > migliore.punti) migliore = { vin: vin, punti: punti };
    }
    (testo || '').toUpperCase().split(/\n/).forEach(function (riga) {
      var pulita = riga.replace(/\b(F\.?I\.?N|V\.?I\.?N|N\.?\s*TELAIO|TELAIO|CHASSIS)\.?\s*[:#\/]?/g, ' ');
      var pezzi = pulita.split(/[\s:;,\/|]+/).map(normalizza).filter(Boolean);
      pezzi.forEach(function (t, k) {
        if (t.length === 17) proponi(t, 5);
        // pezzo troppo lungo: ogni finestra da 17 è un candidato debole
        for (var i = 0; t.length > 17 && i + 17 <= t.length; i++) proponi(t.substr(i, 17), 1);
        // l'OCR a volte spezza il telaio in due o tre parti
        if (k + 1 < pezzi.length && (t + pezzi[k + 1]).length === 17) proponi(t + pezzi[k + 1], 3);
        if (k + 2 < pezzi.length && (t + pezzi[k + 1] + pezzi[k + 2]).length === 17) proponi(t + pezzi[k + 1] + pezzi[k + 2], 3);
      });
    });
    return migliore;
  }
  function aggiornaConta() {
    var n = normalizza(campo.value).length;
    conta.textContent = n + '/17 caratteri' + (n === 17 ? ' ✓' : '');
    conta.classList.toggle('ok', n === 17);
  }
  function trovato(vin, metodo) {
    campo.value = vin;
    aggiornaConta();
    stato.textContent = 'Letto da ' + metodo + '. Confronta con l\'etichetta prima di confermare.';
    precompila(vin);
  }
  function precompila(vin) {
    // se il veicolo è già arrivato dal gestionale, compila marca/modello/colore
    fetch('/api/veicolo?vin=' + encodeURIComponent(vin)).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (d) {
      setTimeout(function () { document.dispatchEvent(new CustomEvent('vin-precompilato', { detail: d || {} })); });
      if (!d) return;
      ['marca', 'modello', 'versione', 'colore'].forEach(function (k) {
        var el = document.querySelector('[name=' + k + ']');
        if (el && !el.value && d[k]) { el.value = d[k]; el.dataset.corretto = '1'; }  // nome ufficiale: non si riformatta
      });
      if (d.da_vin && d.marca) stato.textContent = 'Marca ricavata dal VIN: ' + d.marca + '. Controlla e completa il modello.';
      var targa = document.querySelector('[name=targa]');
      if (targa && !targa.value && d.targa) targa.value = d.targa;
      var tipo = d.tipologia && document.querySelector('[name=tipologia][value=' + d.tipologia + ']');
      if (tipo) tipo.checked = true;
    }).catch(function () {});
  }

  async function barcodeNativo(img) {
    if (!('BarcodeDetector' in window)) return null;
    var det = new BarcodeDetector({ formats: ['code_39', 'code_128', 'data_matrix', 'qr_code', 'pdf417'] });
    var codici = await det.detect(img);
    for (var i = 0; i < codici.length; i++) {
      var v = estrai(codici[i].rawValue);
      if (v) return v.vin;
    }
    return null;
  }
  async function barcodeZxing(url) {
    if (!window.ZXing) return null;
    try {
      var r = await new ZXing.BrowserMultiFormatReader().decodeFromImageUrl(url);
      var v = estrai(r.getText());
      return v && v.vin;
    } catch (e) { return null; }
  }
  // Foto migliorata per l'OCR: ingrandita se piccola, ridotta se enorme, in scala di grigi e con più contrasto.
  function preparaPerOcr(img) {
    var scala = Math.min(2.5, Math.max(1, 1800 / img.naturalWidth), 2400 / img.naturalWidth);
    var c = document.createElement('canvas');
    c.width = Math.round(img.naturalWidth * scala); c.height = Math.round(img.naturalHeight * scala);
    var ctx = c.getContext('2d');
    ctx.drawImage(img, 0, 0, c.width, c.height);
    var dati = ctx.getImageData(0, 0, c.width, c.height), px = dati.data, min = 255, max = 0, i, g;
    for (i = 0; i < px.length; i += 4) {
      g = 0.299 * px[i] + 0.587 * px[i + 1] + 0.114 * px[i + 2];
      px[i] = g; if (g < min) min = g; if (g > max) max = g;
    }
    var ampiezza = Math.max(1, max - min);
    for (i = 0; i < px.length; i += 4) {
      g = Math.max(0, Math.min(255, (px[i] - min) * 255 / ampiezza));
      px[i] = px[i + 1] = px[i + 2] = g;
    }
    ctx.putImageData(dati, 0, 0);
    return c;
  }
  async function ocr(img, file) {
    if (!window.Tesseract) return null;
    var worker = await Tesseract.createWorker('eng');
    var voti = {};  // ogni tentativo vota il suo telaio: vince il più credibile e più letto
    try {
      // I, O e Q non esistono nei VIN ma servono a leggere bene le etichette ("VIN", "FIN")
      await worker.setParameters({ tessedit_char_whitelist: 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:/-. ' });
      var pronta = preparaPerOcr(img);
      var tentativi = [[pronta, '6'], [file, '6'], [pronta, '11'], [file, '11']];
      for (var k = 0; k < tentativi.length; k++) {
        await worker.setParameters({ tessedit_pageseg_mode: tentativi[k][1] });
        var r = await worker.recognize(tentativi[k][0]);
        var v = estrai(r.data.text);
        if (!v) continue;
        if (v.punti >= 8) return v.vin;  // telaio isolato con cifra di controllo giusta: basta così
        var voto = voti[v.vin] || (voti[v.vin] = { punti: 0, volte: 0 });
        voto.punti = Math.max(voto.punti, v.punti); voto.volte++;
      }
    } finally {
      await worker.terminate();
    }
    // prima la qualità del candidato (telaio isolato batte un pezzo di testo), poi quante volte è stato letto
    var scelto = null;
    Object.keys(voti).forEach(function (vin) {
      var a = voti[vin], b = scelto && voti[scelto];
      if (!b || a.punti > b.punti || (a.punti === b.punti && a.volte > b.volte)) scelto = vin;
    });
    return scelto;
  }

  input.addEventListener('change', async function () {
    var file = input.files[0];
    if (!file) return;
    stato.textContent = 'Lettura in corso…';
    var url = URL.createObjectURL(file);
    var img = new Image();
    img.src = url;
    await img.decode();
    try {
      var vin = await barcodeNativo(img);
      if (vin) return trovato(vin, 'codice a barre');
      vin = await barcodeZxing(url);
      if (vin) return trovato(vin, 'codice a barre');
      stato.textContent = 'Nessun codice a barre, leggo i caratteri…';
      vin = await ocr(img, file);
      if (vin) return trovato(vin, 'foto (OCR)');
      stato.textContent = 'VIN non riconosciuto: riprova avvicinandoti all\'etichetta, oppure digitalo a mano.';
    } catch (e) {
      stato.textContent = 'Errore di lettura: digita il VIN a mano.';
    } finally {
      URL.revokeObjectURL(url);
    }
  });
  campo.addEventListener('input', function () {
    var pulito = normalizza(campo.value).replace(/[^A-Z0-9]/g, '').slice(0, 17);
    if (pulito !== campo.value) { var fine = campo.selectionEnd === campo.value.length; campo.value = pulito;
      if (fine) campo.setSelectionRange(pulito.length, pulito.length); }
    aggiornaConta();
    if (pulito.length === 17) precompila(pulito);
  });
  campo.addEventListener('change', function () {
    var v = normalizza(campo.value);
    if (v.length === 17) precompila(v);
  });
  aggiornaConta();
})();
