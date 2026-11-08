"""Offline MCC name reference, not location or band-allocation evidence.

Factual assignments: ITU OB 1117, 2017-02-01, numerical table.
Short names and aliases match user hints. Unknown and shared codes remain
unverified; they never exclude a network from monitoring or learning.
"""
import re
import unicodedata


_ROWS = """
202 Greece
204 Netherlands
206 Belgium
208 France
212 Monaco
213 Andorra
214 Spain
216 Hungary
218 Bosnia and Herzegovina
219 Croatia
220 Serbia
221 Kosovo
222 Italy
225 Vatican City
226 Romania
228 Switzerland
230 Czech Republic|Czechia
231 Slovakia|Slovak Republic
232 Austria
234 United Kingdom|UK|Great Britain
235 United Kingdom|UK|Great Britain
238 Denmark
240 Sweden
242 Norway
244 Finland
246 Lithuania
247 Latvia
248 Estonia
250 Russia|Russian Federation
255 Ukraine
257 Belarus
259 Moldova
260 Poland
262 Germany
266 Gibraltar
268 Portugal
270 Luxembourg
272 Ireland
274 Iceland
276 Albania
278 Malta
280 Cyprus
282 Georgia
283 Armenia
284 Bulgaria
286 Turkey|Turkiye
288 Faroe Islands
290 Greenland
292 San Marino
293 Slovenia
294 North Macedonia|Macedonia
295 Liechtenstein
297 Montenegro
302 Canada
308 Saint Pierre and Miquelon
310 United States|United States of America|USA|US
311 United States|United States of America|USA|US
312 United States|United States of America|USA|US
313 United States|United States of America|USA|US
314 United States|United States of America|USA|US
315 United States|United States of America|USA|US
316 United States|United States of America|USA|US
330 Puerto Rico
332 United States Virgin Islands|US Virgin Islands
334 Mexico
338 Jamaica
340 Guadeloupe|Martinique
342 Barbados
344 Antigua and Barbuda
346 Cayman Islands
348 British Virgin Islands
350 Bermuda
352 Grenada
354 Montserrat
356 Saint Kitts and Nevis
358 Saint Lucia
360 Saint Vincent and the Grenadines
362 Curacao|Sint Maarten|Bonaire, Sint Eustatius and Saba
363 Aruba
364 Bahamas
365 Anguilla
366 Dominica
368 Cuba
370 Dominican Republic
372 Haiti
374 Trinidad and Tobago
376 Turks and Caicos Islands
400 Azerbaijan
401 Kazakhstan
402 Bhutan
404 India
405 India
406 India
410 Pakistan
412 Afghanistan
413 Sri Lanka
414 Myanmar
415 Lebanon
416 Jordan
417 Syria|Syrian Arab Republic
418 Iraq
419 Kuwait
420 Saudi Arabia
421 Yemen
422 Oman
424 United Arab Emirates|UAE
425 Israel
426 Bahrain
427 Qatar
428 Mongolia
429 Nepal
430 United Arab Emirates|UAE
431 United Arab Emirates|UAE
432 Iran
434 Uzbekistan
436 Tajikistan
437 Kyrgyzstan|Kyrgyz Republic
438 Turkmenistan
440 Japan
441 Japan
450 South Korea|Republic of Korea
452 Vietnam|Viet Nam
454 Hong Kong
455 Macao|Macau
456 Cambodia
457 Laos|Lao People's Democratic Republic
460 China
461 China
466 Taiwan
467 North Korea|Democratic People's Republic of Korea
470 Bangladesh
472 Maldives
502 Malaysia
505 Australia
510 Indonesia
514 Timor-Leste
515 Philippines
520 Thailand
525 Singapore
528 Brunei|Brunei Darussalam
530 New Zealand
536 Nauru
537 Papua New Guinea
539 Tonga
540 Solomon Islands
541 Vanuatu
542 Fiji
543 Wallis and Futuna
544 American Samoa
545 Kiribati
546 New Caledonia
547 French Polynesia
548 Cook Islands
549 Samoa
550 Micronesia
551 Marshall Islands
552 Palau
553 Tuvalu
554 Tokelau
555 Niue
602 Egypt
603 Algeria
604 Morocco
605 Tunisia
606 Libya
607 Gambia
608 Senegal
609 Mauritania
610 Mali
611 Guinea
612 Cote d'Ivoire|Ivory Coast
613 Burkina Faso
614 Niger
615 Togo|Togolese Republic
616 Benin
617 Mauritius
618 Liberia
619 Sierra Leone
620 Ghana
621 Nigeria
622 Chad
623 Central African Republic
624 Cameroon
625 Cabo Verde|Cape Verde
626 Sao Tome and Principe
627 Equatorial Guinea
628 Gabon|Gabonese Republic
629 Republic of the Congo
630 Democratic Republic of the Congo|DR Congo
631 Angola
632 Guinea-Bissau
633 Seychelles
634 Sudan
635 Rwanda
636 Ethiopia
637 Somalia
638 Djibouti
639 Kenya
640 Tanzania
641 Uganda
642 Burundi
643 Mozambique
645 Zambia
646 Madagascar
647 French Departments and Territories in the Indian Ocean|Reunion|Mayotte
648 Zimbabwe
649 Namibia
650 Malawi
651 Lesotho
652 Botswana
653 Swaziland|Eswatini
654 Comoros
655 South Africa
657 Eritrea
658 Saint Helena, Ascension and Tristan da Cunha
659 South Sudan
702 Belize
704 Guatemala
706 El Salvador
708 Honduras
710 Nicaragua
712 Costa Rica
714 Panama
716 Peru
722 Argentina
724 Brazil
730 Chile
732 Colombia
734 Venezuela
736 Bolivia
738 Guyana
740 Ecuador
742 French Guiana
744 Paraguay
746 Suriname
748 Uruguay
750 Falkland Islands|Malvinas
"""


def name_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", value.casefold()).encode("ascii", "ignore").decode())


MCC_NAMES = {row.split(" ", 1)[0]: tuple(row.split(" ", 1)[1].split("|"))
             for row in _ROWS.strip().splitlines()}


def country_verdict(suggestion: str, plmn: str) -> tuple[str, str]:
    names = MCC_NAMES.get(plmn[:3], ()) if re.fullmatch(r"[0-9]{5,6}", plmn) else ()
    label = " / ".join(names[:1])
    if not suggestion:
        return "not_requested", label
    if not names:
        return "not_verified", ""
    token = name_token(suggestion)
    if suggestion == plmn[:3] or token in {name_token(n) for n in names}:
        return "confirmed", label
    known = suggestion in MCC_NAMES or any(token == name_token(n) for row in MCC_NAMES.values() for n in row)
    return ("mismatch" if known else "not_verified"), label
