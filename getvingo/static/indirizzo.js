// Indirizzo verificato sulla mappa (macro campi_indirizzo): la via si cerca da sola (OpenStreetMap),
// CAP, provincia e stato si compilano; toccando la mappa o "Sono qui" si compila tutto dal punto.
// Se la via non esiste: contorno rosso e il modulo non parte (anche il server rifiuta senza coordinate).
(function () {
  var blocchi = document.querySelectorAll('[data-indirizzo]');
  if (!blocchi.length) return;
  var NOMINATIM = 'https://nominatim.openstreetmap.org/';
  var leaflet = new Promise(function (ok) {
    if (window.L) return ok();
    var css = document.createElement('link'); css.rel = 'stylesheet';
    css.href = 'https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css'; document.head.appendChild(css);
    var js = document.createElement('script'); js.src = 'https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js';
    js.onload = ok; document.head.appendChild(js);
  });

  blocchi.forEach(function (box) {
    var form = box.closest('form');
    var c = function (n) { return box.querySelector('[data-campo=' + n + ']'); };
    var lat = box.querySelector('[name=lat]'), lon = box.querySelector('[name=lon]');
    var esito = box.querySelector('[data-esito]');
    var obbligatorio = box.hasAttribute('data-obbligatorio');
    var mappa = null, segnaposto = null, timer = null, ultima = '';

    function stato(testo, tipo) {
      esito.textContent = testo || '';
      esito.className = 'indirizzo-esito ' + (tipo || '');
      c('indirizzo').classList.toggle('campo-errore', tipo === 'errore');
    }
    function verificato(la, lo) {
      lat.value = la ? Number(la).toFixed(6) : ''; lon.value = lo ? Number(lo).toFixed(6) : '';
      if (!la && segnaposto && mappa) { mappa.removeLayer(segnaposto); segnaposto = null; }
      if (!la || !mappa) return;
      if (!segnaposto) {
        segnaposto = L.marker([la, lo], { draggable: true }).addTo(mappa);
        segnaposto.on('dragend', function () { var p = segnaposto.getLatLng(); daPunto(p.lat, p.lng); });
      } else segnaposto.setLatLng([la, lo]);
      mappa.setView([la, lo], Math.max(mappa.getZoom(), 17));
    }
    function provincia(a) { return (a['ISO3166-2-lvl6'] || '').replace(/^IT-/, ''); }
    function comuneDi(a) { return a.municipality || a.city || a.town || a.village || ''; }
    function compila(a, sovrascrivi) {
      var imposta = function (n, v) { if (v && (sovrascrivi || !c(n).value)) c(n).value = v; };
      imposta('comune', comuneDi(a)); imposta('cap', a.postcode); imposta('provincia', provincia(a));
      imposta('stato', a.country);
    }
    function cerca() {
      var via = c('indirizzo').value.trim(), comune = c('comune').value.trim();
      if (!via || !comune) return;
      var chiave = via + '|' + comune + '|' + c('stato').value;
      if (chiave === ultima) return;
      ultima = chiave;
      stato('Cerco l’indirizzo…');
      var q = new URLSearchParams({ format: 'jsonv2', addressdetails: 1, limit: 1, 'accept-language': 'it',
                                    street: via, city: comune, country: c('stato').value || 'Italia' });
      fetch(NOMINATIM + 'search?' + q).then(function (r) { return r.json(); }).then(function (ris) {
        var r = ris && ris[0], a = r && r.address;
        if (!a || !a.road) {
          verificato(null);
          return stato('Via non trovata a ' + comune + ': controlla come è scritta, oppure tocca il punto sulla mappa.', 'errore');
        }
        compila(a, true);
        verificato(r.lat, r.lon);
        var civico = /\d/.test(via) && !a.house_number;
        stato(civico ? 'Via trovata (' + a.road + '); il civico non è sulla mappa: controlla il punto.' :
                       'Indirizzo trovato ✓', 'ok');
      }).catch(function () { ultima = ''; stato('Ricerca non riuscita: riprova o tocca il punto sulla mappa.', 'errore'); });
    }
    function daPunto(la, lo) {
      stato('Leggo l’indirizzo del punto…');
      fetch(NOMINATIM + 'reverse?format=jsonv2&addressdetails=1&zoom=18&accept-language=it&lat=' + la + '&lon=' + lo)
        .then(function (r) { return r.json(); }).then(function (d) {
          var a = d && d.address;
          if (!a || !a.road) return stato('Qui non c’è una via: tocca più vicino alla strada.', 'errore');
          c('indirizzo').value = a.road + (a.house_number ? ' ' + a.house_number : '');
          compila(a, true);
          ultima = c('indirizzo').value + '|' + c('comune').value + '|' + c('stato').value;
          verificato(la, lo);
          stato(a.house_number ? 'Indirizzo compilato dal punto ✓' : 'Via compilata dal punto ✓ · aggiungi il numero civico', 'ok');
        }).catch(function () { stato('Lettura non riuscita: riprova.', 'errore'); });
    }

    leaflet.then(function () {
      var div = box.querySelector('[data-mappa]');
      mappa = L.map(div, { zoomControl: true, scrollWheelZoom: false }).setView([45.67, 12.24], 9);
      L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}',
                  { maxZoom: 19, attribution: 'Tiles © Esri' }).addTo(mappa);
      mappa.on('click', function (e) { daPunto(e.latlng.lat, e.latlng.lng); });
      if (lat.value && lon.value) { verificato(lat.value, lon.value); stato('Indirizzo sulla mappa ✓', 'ok'); }
      else if (c('indirizzo').value && c('comune').value) cerca();
    });

    ['indirizzo', 'comune', 'stato'].forEach(function (n) {
      c(n).addEventListener('input', function () {
        verificato(null); stato('');
        clearTimeout(timer); timer = setTimeout(cerca, 1200);  // una ricerca quando si smette di scrivere
      });
      c(n).addEventListener('change', function () { clearTimeout(timer); cerca(); });
    });
    box.querySelector('[data-sono-qui]').addEventListener('click', function () {
      if (!navigator.geolocation) return stato('Questo browser non dà la posizione.', 'errore');
      stato('Rilevo la posizione…');
      navigator.geolocation.getCurrentPosition(function (p) { daPunto(p.coords.latitude, p.coords.longitude); },
        function (e) { stato(e.code === 1 ? 'Hai negato la posizione: consentila nel browser e riprova.' : 'Posizione non disponibile.', 'errore'); },
        { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 });
    });
    if (form) form.addEventListener('submit', function (e) {
      var vuoto = !c('indirizzo').value.trim() && !c('comune').value.trim();
      if (lat.value || (!obbligatorio && vuoto)) return;
      e.preventDefault(); e.stopImmediatePropagation();
      if (!esito.classList.contains('errore')) stato('Indirizzo da verificare: controlla via e comune, oppure tocca il punto sulla mappa.', 'errore');
      box.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }, true);
  });
})();
