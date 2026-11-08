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
      if (GetvingoMappa.google) {
        return cercaGoogle(via + ', ' + comune + ', ' + (c('stato').value || 'Italia')).then(function (ris) {
          var r = ris[0];
          if (!r || !r.via) { verificato(null); return stato('Via non trovata a ' + comune + ': controlla come è scritta, oppure tocca il punto sulla mappa.', 'errore'); }
          c('comune').value = r.comune || comune; c('cap').value = r.cap || c('cap').value;
          c('provincia').value = r.provincia || c('provincia').value; c('stato').value = r.stato || c('stato').value;
          verificato(r.lat, r.lon);
          stato(/\d/.test(via) && !/\d/.test(r.via) ? 'Via trovata; il civico non è sulla mappa: controlla il punto.' : 'Indirizzo trovato ✓', 'ok');
        }).catch(function () { ultima = ''; stato('Ricerca non riuscita: riprova o tocca il punto sulla mappa.', 'errore'); });
      }
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
      if (c('indirizzo').value.trim() && c('comune').value.trim()) {
        // indirizzo già scritto: si corregge solo il punto sulla mappa, via e civico restano quelli
        verificato(la, lo);
        c('indirizzo').classList.remove('campo-errore');
        return stato('Punto aggiornato ✓ · via e civico restano quelli scritti', 'ok');
      }
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

    // ---- "Cerca un luogo": nome di un'azienda o indirizzo; scelto un risultato si compila tutto
    var cercaIn = box.querySelector('[data-cerca]'), lista = box.querySelector('[data-risultati]');
    var campoNome = form && (form.querySelector('[name=nome]') || form.querySelector('[name=nuova_destinazione]') ||
                             form.querySelector('[name=ragione_sociale]'));
    function riempi(r) {
      lista.hidden = true; lista.innerHTML = '';
      c('indirizzo').value = r.via; c('comune').value = r.comune || c('comune').value;
      c('cap').value = r.cap || ''; c('provincia').value = r.provincia || ''; c('stato').value = r.stato || 'Italia';
      if (campoNome && !campoNome.value.trim() && r.nome) campoNome.value = r.nome;
      ultima = c('indirizzo').value + '|' + c('comune').value + '|' + c('stato').value;
      verificato(r.lat, r.lon);
      stato(r.via ? 'Luogo trovato ✓ · controlla i dati' + (/\d/.test(r.via) ? '' : ' e aggiungi il civico') : 'Luogo trovato: scrivi via e civico', r.via ? 'ok' : 'errore');
    }
    function componenti(comp) {
      var t = {}, breve = {};
      (comp || []).forEach(function (x) { (x.types || []).forEach(function (ty) { t[ty] = x.longText; breve[ty] = x.shortText; }); });
      return { via: [t.route, t.street_number].filter(Boolean).join(' '), cap: t.postal_code,
               comune: t.administrative_area_level_3 || t.locality, provincia: breve.administrative_area_level_2, stato: t.country };
    }
    function cercaGoogle(q) {
      return fetch('https://places.googleapis.com/v1/places:searchText', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Goog-Api-Key': GetvingoMappa.chiave,
          'X-Goog-FieldMask': 'places.displayName,places.formattedAddress,places.addressComponents,places.location' },
        body: JSON.stringify({ textQuery: q, languageCode: 'it', regionCode: 'IT', maxResultCount: 6 })
      }).then(function (r) { return r.json(); }).then(function (d) {
        return (d.places || []).map(function (p) {
          var a = componenti(p.addressComponents);
          a.nome = p.displayName && p.displayName.text; a.testo = p.formattedAddress;
          a.lat = p.location.latitude; a.lon = p.location.longitude; return a;
        });
      });
    }
    function cercaOsm(q) {
      var u = NOMINATIM + 'search?' + new URLSearchParams({ format: 'jsonv2', addressdetails: 1, limit: 6,
                                                          'accept-language': 'it', q: q });
      return fetch(u).then(function (r) { return r.json(); }).then(function (ris) {
        return ris.map(function (r) {
          var a = r.address || {};
          return { nome: r.name, testo: r.display_name, via: [a.road, a.house_number].filter(Boolean).join(' '),
                   cap: a.postcode, comune: comuneDi(a), provincia: provincia(a), stato: a.country, lat: r.lat, lon: r.lon };
        });
      });
    }
    function cercaLuogo() {
      var q = cercaIn.value.trim();
      if (q.length < 3) return;
      lista.hidden = false; lista.innerHTML = '<li class="tenue">Cerco…</li>';
      (GetvingoMappa.google ? cercaGoogle(q) : cercaOsm(q)).then(function (ris) {
        lista.innerHTML = '';
        if (!ris.length) { lista.innerHTML = '<li class="tenue">Nessun luogo trovato: prova con nome e comune.</li>'; return; }
        ris.forEach(function (r) {
          var li = document.createElement('li'), b = document.createElement('button');
          b.type = 'button'; b.className = 'risultato-luogo';
          var t = document.createElement('b'); t.textContent = r.nome || r.via; b.appendChild(t);
          var s2 = document.createElement('span'); s2.textContent = r.testo; b.appendChild(s2);
          b.addEventListener('click', function () { riempi(r); });
          li.appendChild(b); lista.appendChild(li);
        });
      }).catch(function () { lista.innerHTML = '<li class="tenue">Ricerca non riuscita: riprova.</li>'; });
    }
    if (cercaIn) {
      box.querySelector('[data-cerca-vai]').addEventListener('click', cercaLuogo);
      cercaIn.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); cercaLuogo(); } });
    }

    leaflet.then(function () {
      var div = box.querySelector('[data-mappa]');
      mappa = L.map(div, { zoomControl: true, scrollWheelZoom: false }).setView([45.67, 12.24], 9);
      GetvingoMappa.sfondo(mappa);
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
