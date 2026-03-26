var BRIEF_CONFIG = {
  studioEmail: 'hello@senns.studio',
  rootFolderName: 'SENNS Brief Intake',
  logoDriveFileId: '1DcxiMRu9jHKDTlgdRahWHjHAqWxBOYtu',
  maxFiles: 10,
  maxFileSizeBytes: 10 * 1024 * 1024,
  maxTotalUploadBytes: 20 * 1024 * 1024,
  allowedMimeTypes: [
    'image/jpeg',
    'image/png',
    'image/webp',
    'application/pdf'
  ]
};

function doGet() {
  return ContentService
    .createTextOutput('SENNS brief backend is running.')
    .setMimeType(ContentService.MimeType.TEXT);
}

function authorizeBriefBackend_() {
  GmailApp.getAliases();
  DriveApp.getRootFolder().getName();
  var doc = DocumentApp.create('SENNS Brief Auth Check');
  var file = DriveApp.getFileById(doc.getId());
  file.setTrashed(true);
  return 'Authorization complete.';
}

function doPost(e) {
  var origin = '*';
  try {
    var payload = parsePayload_(e);
    origin = String(payload.origin || '*');
    var result = processBriefSubmission_(payload);
    return buildPostMessageResponse_({
      type: 'senns-brief-result',
      ok: true,
      result: result
    }, origin);
  } catch (error) {
    return buildPostMessageResponse_({
      type: 'senns-brief-result',
      ok: false,
      message: String((error && error.message) || error || 'Submission failed.')
    }, origin);
  }
}

function parsePayload_(e) {
  var raw = e && e.parameter ? e.parameter.payload : '';
  if (!raw) throw new Error('Missing payload.');
  var payload = JSON.parse(raw);
  if (!payload || typeof payload !== 'object') throw new Error('Invalid payload.');
  return payload;
}

function processBriefSubmission_(payload) {
  validatePayload_(payload);

  var ids = buildBriefIds_(payload.company_name);
  var rootFolder = getOrCreateFolderByName_(BRIEF_CONFIG.rootFolderName);
  var submissionsFolder = getOrCreateChildFolder_(rootFolder, 'submissions');
  var submissionFolder = submissionsFolder.createFolder(ids.submissionFolderName);

  var uploadedFiles = saveUploadedFiles_(submissionFolder, payload.uploaded_files || []);
  var pdfArtifact = buildClientPdf_(payload, uploadedFiles, submissionFolder, ids);
  var markdown = buildStudioMarkdown_(payload, uploadedFiles, pdfArtifact);
  var markdownBlob = Utilities.newBlob(markdown, 'text/markdown', ids.markdownFileName);

  sendClientEmail_(payload, pdfArtifact);
  sendStudioEmail_(payload, uploadedFiles, pdfArtifact, markdownBlob);

  return {
    briefId: ids.briefId,
    pdfDownloadUrl: pdfArtifact.downloadUrl,
    pdfViewUrl: pdfArtifact.viewUrl
  };
}

function validatePayload_(payload) {
  if (!String(payload.company_name || '').trim()) throw new Error('Missing company name.');
  if (!String(payload.contact_name || '').trim()) throw new Error('Missing contact name.');
  if (!isValidEmail_(payload.contact_email)) throw new Error('Missing valid contact email.');
  if (!String(payload.project_description || '').trim()) throw new Error('Missing project description.');
  if (!payload.display || !payload.display.production_type || !payload.display.production_type.length) {
    throw new Error('Missing production type.');
  }

  var uploadedFiles = payload.uploaded_files || [];
  if (uploadedFiles.length > BRIEF_CONFIG.maxFiles) {
    throw new Error('Too many uploaded files.');
  }

  var totalSize = 0;
  uploadedFiles.forEach(function (file) {
    if (!file || typeof file !== 'object') throw new Error('Invalid uploaded file entry.');
    var size = Number(file.size || 0);
    totalSize += size;
    if (!file.name || !file.base64) throw new Error('Uploaded file is incomplete.');
    if (size > BRIEF_CONFIG.maxFileSizeBytes) throw new Error('One of the files exceeds 10MB.');
    var mimeType = String(file.mimeType || '').trim().toLowerCase();
    if (BRIEF_CONFIG.allowedMimeTypes.indexOf(mimeType) === -1) {
      throw new Error('Unsupported uploaded file type.');
    }
  });

  if (totalSize > BRIEF_CONFIG.maxTotalUploadBytes) {
    throw new Error('The total upload size exceeds 20MB.');
  }
}

function saveUploadedFiles_(submissionFolder, uploadedFiles) {
  if (!uploadedFiles.length) return [];

  var uploadsFolder = getOrCreateChildFolder_(submissionFolder, 'uploads');
  return uploadedFiles.map(function (file, index) {
    var blob = Utilities.newBlob(
      Utilities.base64Decode(file.base64),
      String(file.mimeType || 'application/octet-stream'),
      sanitizeFileName_(file.name, index + 1)
    );
    var created = uploadsFolder.createFile(blob);
    return {
      id: created.getId(),
      name: created.getName(),
      viewUrl: created.getUrl()
    };
  });
}

function buildClientPdf_(payload, uploadedFiles, submissionFolder, ids) {
  var labels = getLabels_(payload.lang);
  var doc = DocumentApp.create(ids.docName);
  var body = doc.getBody();

  appendBrandedHeader_(body, labels, payload);
  body.appendHorizontalRule();

  appendSection_(body, labels.projectOverview, [
    [labels.productionType, joinList_(payload.display.production_type)],
    [labels.imageCount, payload.display.image_count],
    [labels.timeline, payload.deadline],
    [labels.usage, joinList_(payload.display.usage)]
  ]);

  appendSection_(body, labels.vision, [
    [labels.projectDescription, payload.project_description],
    [labels.brandAesthetic, payload.display.brand_aesthetic]
  ]);

  appendSection_(body, labels.visualDirection, [
    [labels.mood, payload.mood_keywords],
    [labels.colors, payload.color_palette],
    [labels.setting, payload.display.setting_preference],
    [labels.light, payload.display.light_preference],
    [labels.references, payload.reference_links],
    [labels.admiredBrands, payload.brands_you_admire]
  ]);

  if (uploadedFiles.length) {
    body.appendParagraph(labels.attachments).setHeading(DocumentApp.ParagraphHeading.HEADING2);
    uploadedFiles.forEach(function (file) {
      body.appendParagraph(file.name);
    });
  }

  appendSection_(body, labels.castingStyling, [
    [labels.models, payload.model_preferences],
    [labels.styling, payload.styling_notes],
    [labels.products, payload.product_to_feature]
  ]);

  appendSection_(body, labels.technical, [
    [labels.existingMaterials, payload.display.existing_materials],
    [labels.aspectRatios, joinList_(payload.display.aspect_ratios)],
    [labels.notes, payload.notes]
  ]);

  body.appendParagraph(labels.footer);
  doc.saveAndClose();

  var docFile = DriveApp.getFileById(doc.getId());
  var pdfBlob = docFile.getAs(MimeType.PDF).setName(ids.clientPdfFileName);
  var pdfFile = submissionFolder.createFile(pdfBlob);
  pdfFile.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
  docFile.setTrashed(true);

  return {
    id: pdfFile.getId(),
    fileName: pdfFile.getName(),
    viewUrl: pdfFile.getUrl(),
    downloadUrl: 'https://drive.google.com/uc?export=download&id=' + pdfFile.getId(),
    blob: pdfFile.getBlob()
  };
}

function appendBrandedHeader_(body, labels, payload) {
  var submittedAt = formatDisplayDate_(payload.submitted_at || new Date(), payload.lang);
  var table = body.appendTable([['', '']]);
  var row = table.getRow(0);
  var left = row.getCell(0);
  var right = row.getCell(1);

  try {
    table.setBorderWidth(0);
  } catch (error) {}

  [left, right].forEach(function (cell) {
    try {
      cell.clear();
    } catch (error) {}
    cell.setBackgroundColor('#0E0E10');
  });

  var titleParagraph = left.appendParagraph(labels.title);
  titleParagraph.setAlignment(DocumentApp.HorizontalAlignment.LEFT);
  titleParagraph.editAsText()
    .setFontFamily('Arial')
    .setFontSize(17)
    .setBold(false)
    .setForegroundColor('#FFFFFF');

  var logoBlob = getLogoBlob_();
  if (logoBlob) {
    var image = right.appendImage(logoBlob);
    if (image.getWidth() > 120) {
      var ratio = 120 / image.getWidth();
      image.setWidth(120);
      image.setHeight(Math.round(image.getHeight() * ratio));
    }
    image.getParent().setAlignment(DocumentApp.HorizontalAlignment.RIGHT);
  } else {
    var logoParagraph = right.appendParagraph('SENNS.STUDIO');
    logoParagraph.setAlignment(DocumentApp.HorizontalAlignment.RIGHT);
    logoParagraph.editAsText()
      .setFontFamily('Arial')
      .setFontSize(15)
      .setBold(false)
      .setForegroundColor('#F2F2F2');
  }

  body.appendParagraph(submittedAt)
    .editAsText()
    .setFontFamily('Arial')
    .setFontSize(9)
    .setBold(false)
    .setForegroundColor('#7E828A');
  body.appendParagraph('');
}

function getLogoBlob_() {
  if (!BRIEF_CONFIG.logoDriveFileId) return null;
  try {
    return DriveApp.getFileById(BRIEF_CONFIG.logoDriveFileId).getBlob();
  } catch (error) {
    return null;
  }
}

function buildStudioMarkdown_(payload, uploadedFiles, pdfArtifact) {
  var lines = [
    '# SENNS.STUDIO - Production Brief',
    '',
    '## Client',
    '- Company: ' + payload.company_name,
    '- Contact: ' + payload.contact_name + ', ' + payload.contact_email,
    '- Industry: ' + withFallback_(payload.display.industry, 'not provided'),
    '- Website: ' + withFallback_(payload.website, 'not provided'),
    '',
    '## Project Scope',
    '- Production types: ' + withFallback_(joinList_(payload.display.production_type), 'not specified'),
    '- Estimated deliverables: ' + withFallback_(payload.display.image_count, 'not specified'),
    '- Timeline: ' + withFallback_(payload.deadline, 'not specified'),
    '- Usage channels: ' + withFallback_(joinList_(payload.display.usage), 'not specified'),
    '',
    '## Vision',
    payload.project_description || 'not provided',
    '',
    '## Brand Aesthetic',
    withFallback_(payload.display.brand_aesthetic, 'not specified'),
    '',
    '## Visual Direction',
    '- Mood keywords: ' + withFallback_(payload.mood_keywords, 'not provided'),
    '- Color palette: ' + withFallback_(payload.color_palette, 'not provided'),
    '- Setting: ' + withFallback_(payload.display.setting_preference, 'not provided'),
    '- Light: ' + withFallback_(payload.display.light_preference, 'not provided'),
    '- Reference links: ' + withFallback_(payload.reference_links, 'none'),
    '- Inspirations: ' + withFallback_(payload.brands_you_admire, 'not provided'),
    '',
    '## Uploaded Assets',
    uploadedFiles.length ? '' : '- none'
  ];

  uploadedFiles.forEach(function (file) {
    lines.push('- ' + file.name + ': ' + file.viewUrl);
  });

  lines = lines.concat([
    '',
    '## Casting & Styling',
    '- Model preferences: ' + withFallback_(payload.model_preferences, 'not provided'),
    '- Styling direction: ' + withFallback_(payload.styling_notes, 'not provided'),
    '- Products to feature: ' + withFallback_(payload.product_to_feature, 'not provided'),
    '',
    '## Technical',
    '- Existing materials: ' + withFallback_(payload.display.existing_materials, 'not specified'),
    '- Required aspect ratios: ' + withFallback_(joinList_(payload.display.aspect_ratios), 'not specified'),
    '- Additional notes: ' + withFallback_(payload.notes, 'none'),
    '',
    '## Client PDF',
    '- Download: ' + pdfArtifact.downloadUrl,
    '',
    '---',
    '',
    '## SENNS STUDIO ANALYSIS PROMPT',
    '',
    'Analyze this production brief and provide:',
    '',
    '1. Brief Summary - 3-4 sentences capturing the core of what the client needs.',
    '2. Key Observations - What stands out? What is clear, what is ambiguous, what is missing?',
    '3. Recommended Production Approach - format, session structure, image roles.',
    '4. Session Planning Notes - world, character, light, color direction.',
    '5. Questions to Clarify Before Starting.',
    '6. 3 Sample MJ V7 Prompts following the SENNS 5-pillar structure.',
    '',
    'Context: SENNS.STUDIO uses Midjourney V7 for hero frame generation and a one-hero production workflow. Quality standard is photorealistic, editorial, warm, cinematic, never generic.'
  ]);

  return lines.join('\n');
}

function sendClientEmail_(payload, pdfArtifact) {
  var lang = payload.lang === 'pl' ? 'pl' : 'en';
  var subject = lang === 'pl'
    ? 'SENNS.STUDIO - Twój brief PDF'
    : 'SENNS.STUDIO - Your brief PDF';
  var textBody = lang === 'pl'
    ? 'Cześć!\n\nDzięki wielkie za wypełnienie briefu - super, że mamy już obraz Twojej wizji!\n\nW załączniku podsyłam podsumowanie w PDF. Teraz my bierzemy się za analizę i wrócimy do Ciebie z konkretami w ciągu najbliższych 2 dni roboczych.\n\nGdyby coś się zmieniło - śmiało pisz!\n\nPozdrawiamy,\nZespół SENNS.STUDIO'
    : 'Hi there,\n\nThank you so much for taking the time to fill out the brief. It is great to have a clear picture of your vision.\n\nYou will find the summary PDF attached to this email for your records. We are going to dive into the details now and will get back to you within the next 2 business days with some initial thoughts and next steps.\n\nIf anything else pops into your mind in the meantime, just hit reply!\n\nBest,\nThe SENNS.STUDIO Team';
  var htmlBody = lang === 'pl'
    ? '<p>Cześć!</p><p>Dzięki wielkie za wypełnienie briefu - super, że mamy już obraz Twojej wizji!</p><p>W załączniku podsyłam podsumowanie w PDF. Teraz my bierzemy się za analizę i wrócimy do Ciebie z konkretami w ciągu najbliższych 2 dni roboczych.</p><p>Gdyby coś się zmieniło - śmiało pisz!</p><p>Pozdrawiamy,<br>Zespół SENNS.STUDIO</p>'
    : '<p>Hi there,</p><p>Thank you so much for taking the time to fill out the brief. It is great to have a clear picture of your vision.</p><p>You will find the summary PDF attached to this email for your records. We are going to dive into the details now and will get back to you within the next 2 business days with some initial thoughts and next steps.</p><p>If anything else pops into your mind in the meantime, just hit reply!</p><p>Best,<br>The SENNS.STUDIO Team</p>';

  GmailApp.sendEmail(payload.contact_email, subject, textBody, {
    htmlBody: htmlBody,
    name: 'SENNS.STUDIO',
    replyTo: BRIEF_CONFIG.studioEmail,
    attachments: [pdfArtifact.blob.copyBlob().setName(pdfArtifact.fileName)]
  });
}

function sendStudioEmail_(payload, uploadedFiles, pdfArtifact, markdownBlob) {
  var subject = 'New SENNS brief - ' + payload.company_name;
  var textBody = [
    'A new brief has been submitted.',
    '',
    'Company: ' + payload.company_name,
    'Contact: ' + payload.contact_name + ' (' + payload.contact_email + ')',
    '',
    'Client PDF: ' + pdfArtifact.downloadUrl
  ].join('\n');
  var htmlBody = [
    '<p>A new brief has been submitted.</p>',
    '<p><strong>Company:</strong> ' + escapeHtml_(payload.company_name) + '<br>',
    '<strong>Contact:</strong> ' + escapeHtml_(payload.contact_name) + ' (' + escapeHtml_(payload.contact_email) + ')</p>',
    '<p><strong>Client PDF:</strong> <a href="' + pdfArtifact.downloadUrl + '">download</a></p>',
    uploadedFiles.length ? '<p><strong>Uploaded files:</strong><br>' + uploadedFiles.map(function (file) {
      return '<a href="' + file.viewUrl + '">' + escapeHtml_(file.name) + '</a>';
    }).join('<br>') + '</p>' : ''
  ].join('');

  GmailApp.sendEmail(BRIEF_CONFIG.studioEmail, subject, textBody, {
    htmlBody: htmlBody,
    name: 'SENNS.STUDIO',
    replyTo: BRIEF_CONFIG.studioEmail,
    attachments: [markdownBlob]
  });
}


function appendSection_(body, title, rows) {
  var filtered = rows.filter(function (row) {
    return String(row[1] || '').trim();
  });
  if (!filtered.length) return;
  body.appendParagraph(title).setHeading(DocumentApp.ParagraphHeading.HEADING2);
  filtered.forEach(function (row) {
    body.appendParagraph(row[0] + ' ' + row[1]);
  });
}

function getLabels_(lang) {
  return lang === 'pl' ? {
    title: 'Brief Projektowy',
    date: 'Data:',
    industry: 'Branża:',
    website: 'Strona:',
    projectOverview: 'Informacje o projekcie',
    productionType: 'Typ produkcji:',
    imageCount: 'Szacunkowa liczba zdjęć:',
    timeline: 'Termin:',
    usage: 'Użycie:',
    vision: 'Wizja',
    projectDescription: 'Opis projektu:',
    brandAesthetic: 'Estetyka marki:',
    visualDirection: 'Kierunek wizualny',
    mood: 'Nastrój:',
    colors: 'Kolory:',
    setting: 'Sceneria:',
    light: 'Światło:',
    references: 'Referencje:',
    admiredBrands: 'Inspiracje:',
    attachments: 'Załączniki',
    castingStyling: 'Casting i stylizacja',
    models: 'Modele:',
    styling: 'Stylizacja:',
    products: 'Produkty:',
    technical: 'Techniczne',
    existingMaterials: 'Materiały:',
    aspectRatios: 'Formaty:',
    notes: 'Notatki:',
    footer: 'senns.studio | hello@senns.studio'
  } : {
    title: 'Project Brief',
    date: 'Date:',
    industry: 'Industry:',
    website: 'Website:',
    projectOverview: 'Project overview',
    productionType: 'Production type:',
    imageCount: 'Estimated deliverables:',
    timeline: 'Timeline:',
    usage: 'Usage:',
    vision: 'Vision',
    projectDescription: 'Project description:',
    brandAesthetic: 'Brand aesthetic:',
    visualDirection: 'Visual direction',
    mood: 'Mood:',
    colors: 'Colors:',
    setting: 'Setting:',
    light: 'Light:',
    references: 'References:',
    admiredBrands: 'Inspirations:',
    attachments: 'Attachments',
    castingStyling: 'Casting & styling',
    models: 'Model preferences:',
    styling: 'Styling:',
    products: 'Products:',
    technical: 'Technical',
    existingMaterials: 'Existing materials:',
    aspectRatios: 'Aspect ratios:',
    notes: 'Notes:',
    footer: 'senns.studio | hello@senns.studio'
  };
}

function buildBriefIds_(companyName) {
  var stamp = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd_HH-mm-ss');
  var slug = slugify_(companyName);
  var briefId = stamp + '__' + slug;
  return {
    briefId: briefId,
    slug: slug,
    submissionFolderName: briefId,
    docName: 'brief_doc_' + briefId,
    clientPdfFileName: 'brief_' + slug + '_' + stamp + '.pdf',
    markdownFileName: 'brief_' + slug + '_' + stamp + '.md'
  };
}

function getOrCreateFolderByName_(name) {
  var folders = DriveApp.getFoldersByName(name);
  return folders.hasNext() ? folders.next() : DriveApp.createFolder(name);
}

function getOrCreateChildFolder_(parent, name) {
  var folders = parent.getFoldersByName(name);
  return folders.hasNext() ? folders.next() : parent.createFolder(name);
}

function sanitizeFileName_(name, index) {
  var safe = String(name || 'upload_' + index)
    .replace(/[\\/:*?"<>|#%]+/g, '-')
    .replace(/\s+/g, ' ')
    .trim();
  return safe || ('upload_' + index);
}

function slugify_(value) {
  return String(value || 'client')
    .toLowerCase()
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '') || 'client';
}

function isValidEmail_(value) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(value || '').trim());
}

function joinList_(items) {
  return (items || []).filter(Boolean).join(', ');
}

function withFallback_(value, fallback) {
  var text = String(value || '').trim();
  return text || fallback;
}

function formatDisplayDate_(value, lang) {
  var date = value ? new Date(value) : new Date();
  if (lang === 'pl') {
    return Utilities.formatDate(date, Session.getScriptTimeZone(), 'dd.MM.yyyy HH:mm');
  }
  return Utilities.formatDate(date, Session.getScriptTimeZone(), 'yyyy-MM-dd HH:mm');
}

function buildPostMessageResponse_(payload, origin) {
  var safeOrigin = JSON.stringify(origin || '*');
  var safePayload = JSON.stringify(payload)
    .replace(/</g, '\\u003c')
    .replace(/>/g, '\\u003e')
    .replace(/&/g, '\\u0026');
  return HtmlService
    .createHtmlOutput('<!DOCTYPE html><html><body><script>window.top.postMessage(' + safePayload + ', ' + safeOrigin + ');</script></body></html>')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

function escapeHtml_(value) {
  return String(value || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
