'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useMutation } from '@tanstack/react-query';
import * as api from '@/lib/api';
import { useCommodityGroups } from '@/lib/hooks/useCommodityGroups';
import { Alert, Button, Input, Loader, TextArea } from '@/components/base';

interface OrderLine extends api.OrderLineInput {
  localId: string;
  selectedArticle: api.ArticleMatch | null;
  originalValues: api.OrderLineInput | null;
}

interface Adjustment extends api.AdjustmentInput {
  id: string;
  lineIndex: number | null;
}

type FormErrors = Record<string, string>;

const roundCurrency = (value: number) => Math.round((value + Number.EPSILON) * 100) / 100;

const asNumber = (value: string) => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
};

const newLineId = () => `${Date.now()}-${Math.random()}`;
const normalizedName = (value: string) => value.trim().toLocaleLowerCase().replace(/\s+/g, ' ');

function formatMoney(value: number | string, currency: string): string {
  const amount = Number(value);
  if (!Number.isFinite(amount)) return String(value);
  try {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency }).format(amount);
  } catch {
    return `${currency} ${amount.toFixed(2)}`;
  }
}

interface RequestGroupPreview {
  key: string;
  supplierName: string;
  currency: string;
  count: number;
  subtotal: number;
  total: number;
}

function buildRequestGroups(
  lines: OrderLine[],
  originalSupplier: string,
  adjustments: Adjustment[],
  quoteCurrency: string
): RequestGroupPreview[] {
  const supplierIdsByName = new Map<string, Set<string>>();
  for (const line of lines) {
    if (!line.selectedArticle?.supplier_name) continue;
    const name = normalizedName(line.selectedArticle.supplier_name);
    const ids = supplierIdsByName.get(name) || new Set<string>();
    ids.add(line.selectedArticle.supplier_id);
    supplierIdsByName.set(name, ids);
  }

  const groups = new Map<string, RequestGroupPreview>();
  for (const line of lines) {
    const article = line.selectedArticle;
    const matchingIds = supplierIdsByName.get(normalizedName(originalSupplier));
    const matchedOriginal = !article && matchingIds?.size === 1;
    const key = article
      ? `supplier:${article.supplier_id}:${article.currency}`
      : matchedOriginal
        ? `supplier:${[...matchingIds!][0]}:${line.currency}`
        : `original:${normalizedName(originalSupplier)}:${line.currency}`;
    const supplierName = article?.supplier_name || (matchedOriginal ? originalSupplier.trim() : originalSupplier.trim());
    const currency = article?.currency || line.currency;
    const group = groups.get(key) || { key, supplierName, currency, count: 0, subtotal: 0, total: 0 };
    group.count += 1;
    group.subtotal = roundCurrency(group.subtotal + line.totalPrice);
    group.total = roundCurrency(group.total + line.totalPrice);
    groups.set(key, group);
  }
  const result = [...groups.values()];
  const originalSupplierGroups = result.filter((group) => normalizedName(group.supplierName) === normalizedName(originalSupplier));
  const orderAdjustments = adjustments.filter((item) => item.scope === 'order');
  if (originalSupplierGroups.length === 1 && originalSupplierGroups[0].currency === quoteCurrency) {
    const adjustmentTotal = orderAdjustments.reduce((sum, item) =>
      sum + (item.kind === 'discount' ? -item.amount : item.amount), 0);
    originalSupplierGroups[0].total = roundCurrency(originalSupplierGroups[0].total + adjustmentTotal);
  }
  return result;
}

function prorateAdjustmentsForLineChange(
  adjustments: Adjustment[],
  previousLines: OrderLine[],
  nextLines: OrderLine[],
  changedIndex: number,
  originalSupplier: string,
  quoteCurrency: string
): Adjustment[] {
  const belongsToOriginalSupplier = (line: OrderLine) =>
    line.currency === quoteCurrency
    && (!line.selectedArticle
      || (!!line.selectedArticle.supplier_name
        && normalizedName(line.selectedArticle.supplier_name) === normalizedName(originalSupplier)));
  const oldSubtotal = previousLines
    .filter(belongsToOriginalSupplier)
    .reduce((sum, line) => sum + line.totalPrice, 0);
  const newSubtotal = nextLines
    .filter(belongsToOriginalSupplier)
    .reduce((sum, line) => sum + line.totalPrice, 0);
  const oldLine = previousLines[changedIndex];
  const newLine = nextLines[changedIndex];
  return adjustments.map((adjustment) => {
    if (adjustment.scope === 'order' && oldSubtotal > 0) {
      return { ...adjustment, amount: roundCurrency(adjustment.amount * newSubtotal / oldSubtotal) };
    }
    if (adjustment.scope === 'line' && adjustment.lineIndex === changedIndex
      && oldLine?.currency === newLine?.currency && oldLine?.totalPrice > 0) {
      return { ...adjustment, amount: roundCurrency(adjustment.amount * newLine.totalPrice / oldLine.totalPrice) };
    }
    return adjustment;
  });
}

async function classifyLineItems(
  lines: Array<{ positionDescription: string; totalPrice: number; currency: string }>
): Promise<api.CategoryAssignment[]> {
  const assignments: api.CategoryAssignment[] = [];
  for (let start = 0; start < lines.length; start += 50) {
    const batch = lines.slice(start, start + 50);
    try {
      const result = await api.extraction.classifyLines(batch);
      assignments.push(...result.lines.map((item) => ({ ...item, index: item.index + start })));
    } catch {
      assignments.push(...batch.map((_, index) => ({
        index: start + index,
        commodityGroupId: null,
        status: 'needs_review' as const,
        error: 'Category service is unavailable.',
      })));
      break;
    }
  }
  return assignments;
}

interface CatalogSuggestionsProps {
  description: string;
  selectedArticle: api.ArticleMatch | null;
  onSelect: (article: api.ArticleMatch) => void;
  onClearSelection: () => void;
}

function CatalogSuggestions({
  description,
  selectedArticle,
  onSelect,
  onClearSelection,
}: CatalogSuggestionsProps) {
  const pageSize = 8;
  const [matches, setMatches] = useState<api.ArticleMatch[]>([]);
  const [totalMatches, setTotalMatches] = useState(0);
  const [matchOffset, setMatchOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [searchError, setSearchError] = useState('');
  const [noResults, setNoResults] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [customSearch, setCustomSearch] = useState(false);
  const [searchText, setSearchText] = useState(description);
  const activeRequest = useRef(0);

  useEffect(() => {
    if (!customSearch) setSearchText(description);
  }, [customSearch, description]);

  useEffect(() => {
    if (!noResults) return;
    const timer = window.setTimeout(() => setNoResults(false), 3000);
    return () => window.clearTimeout(timer);
  }, [noResults]);

  useEffect(() => {
    if (!selectedArticle) return;
    activeRequest.current += 1;
    setMatches([]);
    setTotalMatches(0);
    setMatchOffset(0);
    setLoading(false);
    setSearchError('');
    setNoResults(false);
  }, [selectedArticle]);

  const runSearch = async (value: string, offset = 0) => {
    // A long extracted description is still useful for keyword matching, but
    // keeping it within the API limit prevents a validation error.
    const term = value.trim().slice(0, 1000);
    const requestId = ++activeRequest.current;
    setMatches([]);
    setTotalMatches(0);
    setMatchOffset(offset);
    setNoResults(false);
    setSearchError('');
    setSearchOpen(true);
    setSearchText(term);
    if (term.length < 2) {
      setSearchError('Enter at least two characters to search the catalog.');
      setLoading(false);
      return;
    }

    setLoading(true);
    try {
      const result = await api.articles.suggest({ search: term, limit: pageSize, offset });
      if (requestId !== activeRequest.current) return;
      setMatches(result.items);
      setTotalMatches(result.total);
      setMatchOffset(result.offset);
      setNoResults(result.total === 0);
    } catch {
      if (requestId === activeRequest.current) {
        setSearchError('Catalog search is unavailable. You can still enter the item manually.');
      }
    } finally {
      if (requestId === activeRequest.current) setLoading(false);
    }
  };

  const closeResults = () => {
    activeRequest.current += 1;
    setMatches([]);
    setTotalMatches(0);
    setMatchOffset(0);
    setNoResults(false);
    setSearchError('');
    setSearchOpen(false);
    setLoading(false);
  };

  const useDescriptionSearch = () => {
    setCustomSearch(false);
    setSearchText(description);
  };

  if (selectedArticle) {
    return (
      <div className="mt-3 rounded-lg border border-green-200 bg-green-50 p-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="text-sm font-medium text-green-900">Catalog item selected · {selectedArticle.article_number}</p>
            <p className="mt-1 text-sm text-green-900">{selectedArticle.supplier_name} · {formatMoney(selectedArticle.unit_price, selectedArticle.currency)} / {selectedArticle.unit}</p>
          </div>
          <Button type="button" variant="outline" size="sm" onClick={onClearSelection}>Remove catalog choice</Button>
        </div>
      </div>
    );
  }

  return (
    <div className="mt-3">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
        <div className="flex-1">
          <Input
            label="Catalog search keyword"
            helperText="Defaults to the item description. Change it to search with different words."
            value={searchText}
            onChange={(event) => { setCustomSearch(true); setSearchText(event.target.value); }}
            placeholder="Article number or keywords"
          />
        </div>
        <Button type="button" variant="outline" size="sm" disabled={loading} onClick={() => { void runSearch(searchText); }}>Search Catalog</Button>
        {searchOpen && <Button type="button" variant="outline" size="sm" onClick={closeResults}>Close</Button>}
        {loading && <span className="text-xs text-ink/50">Searching catalog…</span>}
      </div>
      {customSearch && <button type="button" className="mt-1 text-xs text-accent-deep underline" onClick={useDescriptionSearch}>Use item description</button>}
      {searchError && <p className="mt-2 text-sm text-amber-700">{searchError}</p>}
      {matches.length > 0 && (
        <div className="mt-3 space-y-2">
          <p className="text-xs font-medium uppercase tracking-wide text-ink/45">
            Catalog suggestions · showing {matchOffset + 1}–{matchOffset + matches.length} of {totalMatches}
          </p>
          {matches.map((article) => (
            <div key={article.id} className="flex flex-col gap-3 rounded-lg border border-line bg-white p-3 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <p className="text-sm font-medium text-ink">{article.description}</p>
                <p className="mt-1 text-xs text-ink/60">{article.supplier_name} · {article.article_number} · {formatMoney(article.unit_price, article.currency)} / {article.unit}</p>
                <p className="mt-1 text-xs text-accent-deep">
                  {article.match_type === 'article_number' ? (article.score === 1 ? 'Exact article number' : 'Partial article-number match') : article.match_type === 'exact_description' ? 'Exact description match' : `Keyword match · matched: ${article.matched_terms.join(', ')} · ${Math.round(article.score * 100)}%`}
                </p>
              </div>
              <Button type="button" size="sm" onClick={() => onSelect(article)}>Use this item</Button>
            </div>
          ))}
          {totalMatches > pageSize && (
            <div className="flex items-center justify-between pt-1">
              <Button type="button" variant="outline" size="sm" disabled={loading || matchOffset === 0} onClick={() => { void runSearch(searchText, Math.max(0, matchOffset - pageSize)); }}>Previous</Button>
              <span className="text-xs text-ink/55">Page {Math.floor(matchOffset / pageSize) + 1} of {Math.ceil(totalMatches / pageSize)}</span>
              <Button type="button" variant="outline" size="sm" disabled={loading || matchOffset + pageSize >= totalMatches} onClick={() => { void runSearch(searchText, matchOffset + pageSize); }}>Next</Button>
            </div>
          )}
        </div>
      )}
      {noResults && (
        <p role="status" className="mt-3 text-sm text-ink/55">No catalog matches found. You can keep the entered item or try different search terms.</p>
      )}
    </div>
  );
}

const newAdjustment = (scope: 'line' | 'order', lineIndex: number | null = null): Adjustment => ({
  id: `${Date.now()}-${Math.random()}`,
  kind: 'tax',
  description: '',
  amount: 0,
  scope,
  lineIndex,
});

function normalizeAdjustment(input: api.AdjustmentInput): Adjustment {
  return {
    id: `${Date.now()}-${Math.random()}`,
    kind: input.kind === 'discount' || input.kind === 'charge' ? input.kind : 'tax',
    description: input.description || '',
    amount: Number(input.amount) || 0,
    scope: input.scope === 'line' ? 'line' : 'order',
    lineIndex: input.scope === 'line' ? (input.lineIndex ?? null) : null,
  };
}

export default function NewRequestPage() {
  const router = useRouter();
  const { data: commodityGroups } = useCommodityGroups();

  const [requestorName, setRequestorName] = useState('');
  const [title, setTitle] = useState('');
  const [vendorName, setVendorName] = useState('');
  const [vatId, setVatId] = useState('');
  const [department, setDepartment] = useState('');
  const [orderLines, setOrderLines] = useState<OrderLine[]>([]);
  const [adjustments, setAdjustments] = useState<Adjustment[]>([]);
  // Kept separate from the calculated total so that quote mismatches are visible.
  const [quotedTotal, setQuotedTotal] = useState(0);
  const [quoteCurrency, setQuoteCurrency] = useState('USD');
  const [hasExtractedQuote, setHasExtractedQuote] = useState(false);
  const [discrepancyNote, setDiscrepancyNote] = useState('');
  const [requiredFields, setRequiredFields] = useState<api.ConfigurableRequiredField[]>([]);

  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [warning, setWarning] = useState('');
  const [formErrors, setFormErrors] = useState<FormErrors>({});
  const [extracting, setExtracting] = useState(false);
  const [classifyingCategories, setClassifyingCategories] = useState(false);
  const [classifyingLineIds, setClassifyingLineIds] = useState<Set<string>>(new Set());
  const [submitting, setSubmitting] = useState(false);
  const categoryRequestIds = useRef(new Map<string, number>());

  const invalidateLineClassification = (lineId: string) => {
    categoryRequestIds.current.set(lineId, (categoryRequestIds.current.get(lineId) || 0) + 1);
    setClassifyingLineIds((current) => {
      if (!current.has(lineId)) return current;
      const next = new Set(current);
      next.delete(lineId);
      return next;
    });
  };

  const classifySelectedCatalogItem = async (
    lineId: string,
    article: api.ArticleMatch,
    totalPrice: number
  ) => {
    const requestId = (categoryRequestIds.current.get(lineId) || 0) + 1;
    categoryRequestIds.current.set(lineId, requestId);
    setClassifyingLineIds((current) => new Set(current).add(lineId));
    try {
      const [assignment] = await classifyLineItems([{
        positionDescription: article.description,
        totalPrice,
        currency: article.currency,
      }]);
      if (categoryRequestIds.current.get(lineId) !== requestId) return;
      setOrderLines((current) => current.map((line) => {
        if (line.localId !== lineId || line.selectedArticle?.id !== article.id) return line;
        return {
          ...line,
          commodityGroupId: assignment?.status === 'assigned' ? assignment.commodityGroupId : null,
          classificationSource: assignment?.status === 'assigned' ? 'llm' : 'manual',
        };
      }));
      if (assignment?.status !== 'assigned') {
        setWarning('Catalog item selected. Review and choose its category manually.');
      }
    } finally {
      if (categoryRequestIds.current.get(lineId) === requestId) {
        setClassifyingLineIds((current) => {
          const next = new Set(current);
          next.delete(lineId);
          return next;
        });
      }
    }
  };

  useEffect(() => {
    api.organizations.getSettings()
      .then((settings) => setRequiredFields(settings.required_fields))
      .catch(() => undefined);
  }, []);

  const requestGroups = useMemo(
    () => buildRequestGroups(orderLines, vendorName, adjustments, quoteCurrency),
    [orderLines, vendorName, adjustments, quoteCurrency]
  );
  const hasCatalogSelection = orderLines.some((line) => line.selectedArticle !== null);
  const hasUnmatchedLines = orderLines.some((line) => line.selectedArticle === null);
  const hasMultipleGroups = requestGroups.length > 1;
  const canCompareQuoteTotal = hasExtractedQuote && !hasCatalogSelection && !hasMultipleGroups;

  const calculation = useMemo(() => {
    const itemSubtotal = roundCurrency(orderLines.reduce((sum, line) => sum + line.totalPrice, 0));
    const quoteAdjustments = adjustments.filter((item) => item.scope === 'order');
    const additions = roundCurrency(
      quoteAdjustments
        .filter((item) => item.kind === 'tax' || item.kind === 'charge')
        .reduce((sum, item) => sum + item.amount, 0)
    );
    const discounts = roundCurrency(
      quoteAdjustments.filter((item) => item.kind === 'discount').reduce((sum, item) => sum + item.amount, 0)
    );
    return { itemSubtotal, additions, discounts, total: roundCurrency(itemSubtotal + additions - discounts) };
  }, [adjustments, orderLines]);

  const hasTotalMismatch = canCompareQuoteTotal && Math.abs(quotedTotal - calculation.total) > 0.005;
  const requiresVatId = requiredFields.includes('vat_id');
  const requiresDepartment = requiredFields.includes('department');

  const clearFieldError = (field: string) => {
    setFormErrors((current) => {
      if (!current[field]) return current;
      const { [field]: _removed, ...remaining } = current;
      return remaining;
    });
  };

  const parsePDFMutation = useMutation({
    mutationFn: async (uploadedFile: File) => {
      const result = await api.pdf.parse(uploadedFile);
      if (!result.text) throw new Error('The PDF does not contain readable text.');
      return result.text;
    },
  });

  const applyExtractionWarnings = (missingFields: string[] | undefined) => {
    if (!missingFields?.length) return;
    setWarning(`Could not extract: ${missingFields.join(', ')}. Please review and complete these values.`);
    setFormErrors((current) => {
      const next = { ...current };
      if (missingFields.includes('Title/Short Description')) next.title = 'Title is required.';
      if (missingFields.includes('Vendor Name')) next.vendorName = 'Vendor name is required.';
      if (missingFields.includes('Tax ID') && requiresVatId) next.vatId = 'Tax ID is required.';
      if (missingFields.includes('Department') && requiresDepartment) next.department = 'Department is required.';
      if (missingFields.includes('Order Lines')) next.orderLines = 'Add at least one order line.';
      if (missingFields.includes('Total Cost')) next.quotedTotal = 'Quoted total is required.';
      return next;
    });
  };

  const handleFileChange = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const uploadedFile = event.target.files?.[0];
    if (!uploadedFile) return;
    if (uploadedFile.type !== 'application/pdf') {
      setError('Please upload a PDF file.');
      return;
    }

    setFile(uploadedFile);
    setError('');
    setSuccess('');
    setWarning('');
    setHasExtractedQuote(false);
    setFormErrors({});
    setExtracting(true);

    try {
      const pdfText = await parsePDFMutation.mutateAsync(uploadedFile);
      const extractionResult = await api.extraction.extract(pdfText);
      if (!extractionResult.success || !extractionResult.data) {
        throw new Error(extractionResult.error || 'We could not extract this quote.');
      }

      const extracted = extractionResult.data;
      setTitle(extracted.title || '');
      setVendorName(extracted.vendorName || '');
      setVatId(extracted.vatId || '');
      setDepartment(extracted.department || '');
      const extractedLines: OrderLine[] = (extracted.orderLines || []).map((line) => ({
        localId: newLineId(),
        positionDescription: line.positionDescription || '',
        unitPrice: Number(line.unitPrice) || 0,
        amount: Number(line.amount) || 0,
        unit: line.unit || '',
        totalPrice: Number(line.totalPrice) || 0,
        currency: line.currency || 'USD',
        catalogArticleId: null,
        catalogArticleNumber: null,
        selectedArticle: null,
        originalValues: null,
        commodityGroupId: null,
        classificationSource: 'manual',
      }));
      setOrderLines(extractedLines);
      if (extractedLines.length) {
        setClassifyingCategories(true);
        try {
          const linesToClassify = extractedLines.filter((line) => line.positionDescription.trim().length > 0);
          const assignments = await classifyLineItems(linesToClassify.map((line) => ({
            positionDescription: line.positionDescription,
            totalPrice: line.totalPrice,
            currency: line.currency,
          })));
          const byLineId = new Map(linesToClassify.map((line, index) => [
            line.localId,
            { assignment: assignments.find((item) => item.index === index), line },
          ]));
          setOrderLines((current) => current.map((line) => {
            const classified = byLineId.get(line.localId);
            if (!classified
              || line.positionDescription !== classified.line.positionDescription
              || line.totalPrice !== classified.line.totalPrice) return line;
            const assignment = classified.assignment;
            return {
              ...line,
              commodityGroupId: assignment?.commodityGroupId ?? null,
              classificationSource: assignment?.status === 'assigned' ? 'llm' : 'manual',
            };
          }));
          if (assignments.some((item) => item.status !== 'assigned')) {
            setWarning('Quote extracted. Some item categories need manual review.');
          }
        } catch {
          setWarning('Quote extracted, but categories could not be assigned. Select them manually or retry classification.');
        } finally {
          setClassifyingCategories(false);
        }
      }
      setAdjustments((extracted.adjustments || []).map(normalizeAdjustment));
      setQuotedTotal(Number(extracted.totalCost) || 0);
      setQuoteCurrency(extracted.orderLines?.[0]?.currency || 'USD');
      setHasExtractedQuote(true);
      setDiscrepancyNote('');
      applyExtractionWarnings(extractionResult.missingFields);
      setSuccess('Quote extracted. Review the fields, item categories, taxes, and discounts before creating the request.');
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : '';
      setError(message || 'We could not read this quote. Please enter the details manually.');
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } finally {
      setExtracting(false);
    }
  };

  const addOrderLine = () => {
    setOrderLines((current) => [
      ...current,
      {
        localId: newLineId(),
        positionDescription: '',
        unitPrice: 0,
        amount: 1,
        unit: '',
        totalPrice: 0,
        currency: 'USD',
        catalogArticleId: null,
        catalogArticleNumber: null,
        selectedArticle: null,
        originalValues: null,
        commodityGroupId: null,
        classificationSource: 'manual',
      },
    ]);
    clearFieldError('orderLines');
  };

  const updateOrderLine = (index: number, field: keyof api.OrderLineInput, value: string | number) => {
    if (['positionDescription', 'unitPrice', 'amount', 'currency'].includes(field)) {
      const lineId = orderLines[index]?.localId;
      if (lineId) invalidateLineClassification(lineId);
    }
    if (field === 'currency' && !hasCatalogSelection) {
      const nextCurrency = String(value).toUpperCase().slice(0, 3);
      const allLinesWillMatch = orderLines.every((line, lineIndex) =>
        lineIndex === index || line.currency === nextCurrency
      );
      if (allLinesWillMatch) setQuoteCurrency(nextCurrency);
    }
    const nextLines = orderLines.map((line, lineIndex) => {
      if (lineIndex !== index) return line;
      const updated = { ...line, [field]: value };
      if (field === 'positionDescription') {
        updated.commodityGroupId = null;
        updated.classificationSource = 'manual';
      }
      if (field === 'unitPrice' || field === 'amount') {
        updated.commodityGroupId = null;
        updated.classificationSource = 'manual';
      }
      if (field === 'currency') updated.currency = String(value).toUpperCase().slice(0, 3);
      if (field === 'unitPrice' || field === 'amount') {
        updated.totalPrice = roundCurrency(Number(updated.unitPrice) * Number(updated.amount));
      }
      return updated;
    });
    setOrderLines(nextLines);
    if (field === 'unitPrice' || field === 'amount' || field === 'totalPrice') {
      setAdjustments((current) => prorateAdjustmentsForLineChange(
        current, orderLines, nextLines, index, vendorName, quoteCurrency
      ));
    }
    clearFieldError('orderLines');
  };

  const selectCatalogArticle = (index: number, article: api.ArticleMatch) => {
    const selectedLine = orderLines[index];
    if (!selectedLine) return;
    const nextLines: OrderLine[] = orderLines.map((line, lineIndex) => {
      if (lineIndex !== index) return line;
      const originalValues = line.originalValues || {
        positionDescription: line.positionDescription,
        unitPrice: line.unitPrice,
        amount: line.amount,
        unit: line.unit,
        totalPrice: line.totalPrice,
        currency: line.currency,
        catalogArticleId: null,
        catalogArticleNumber: null,
      };
      const unitPrice = Number(article.unit_price);
      return {
        ...line,
        positionDescription: article.description,
        unitPrice,
        unit: article.unit,
        currency: article.currency,
        totalPrice: roundCurrency(unitPrice * Number(line.amount)),
        catalogArticleId: article.id,
        catalogArticleNumber: article.article_number,
        selectedArticle: article,
        originalValues,
        commodityGroupId: null,
        classificationSource: 'manual',
      };
    });
    setOrderLines(nextLines);
    setAdjustments((current) => prorateAdjustmentsForLineChange(
      current, orderLines, nextLines, index, vendorName, quoteCurrency
    ));
    void classifySelectedCatalogItem(
      selectedLine.localId,
      article,
      roundCurrency(Number(article.unit_price) * Number(selectedLine.amount))
    );
    if (orderLines.every((line, lineIndex) => lineIndex === index || line.selectedArticle !== null)) {
      clearFieldError('vendorName');
      clearFieldError('vatId');
    }
    clearFieldError('orderLines');
  };

  const clearCatalogArticle = (index: number) => {
    const lineId = orderLines[index]?.localId;
    if (lineId) invalidateLineClassification(lineId);
    const nextLines = orderLines.map((line, lineIndex) => {
      if (lineIndex !== index) return line;
      const original = line.originalValues;
      if (!original) {
        return { ...line, catalogArticleId: null, catalogArticleNumber: null, selectedArticle: null };
      }
      return {
        ...line,
        ...original,
        amount: line.amount,
        totalPrice: line.amount === original.amount
          ? original.totalPrice
          : roundCurrency(original.unitPrice * Number(line.amount)),
        catalogArticleId: null,
        catalogArticleNumber: null,
        selectedArticle: null,
        originalValues: null,
      };
    });
    setOrderLines(nextLines);
    setAdjustments((current) => prorateAdjustmentsForLineChange(
      current, orderLines, nextLines, index, vendorName, quoteCurrency
    ));
  };

  const removeOrderLine = (index: number) => {
    const removedLineId = orderLines[index]?.localId;
    if (removedLineId) invalidateLineClassification(removedLineId);
    setOrderLines((current) => current.filter((_, lineIndex) => lineIndex !== index));
    const remainingLines = orderLines.filter((_, lineIndex) => lineIndex !== index);
    if (remainingLines.every((line) => line.selectedArticle !== null)) {
      clearFieldError('vendorName');
      clearFieldError('vatId');
    }
    setAdjustments((current) => current
      .filter((item) => item.scope !== 'line' || item.lineIndex !== index)
      .map((item) => (
        item.scope === 'line' && item.lineIndex !== null && item.lineIndex > index
          ? { ...item, lineIndex: item.lineIndex - 1 }
          : item
      )));
  };

  const classifyAllLines = async () => {
    const targets = orderLines
      .map((line) => ({ line }))
      .filter(({ line }) => line.positionDescription.trim().length > 0);
    if (!targets.length || classifyingCategories) return;
    setClassifyingCategories(true);
    setWarning('');
    try {
      const assignments = await classifyLineItems(targets.map(({ line }) => ({
        positionDescription: line.positionDescription,
        totalPrice: line.totalPrice,
        currency: line.currency,
      })));
      const byLineId = new Map<string, { line: OrderLine; assignment: api.CategoryAssignment | undefined }>();
      targets.forEach(({ line }, localIndex) => {
        byLineId.set(line.localId, { line, assignment: assignments[localIndex] });
      });
      setOrderLines((current) => current.map((line) => {
        const classified = byLineId.get(line.localId);
        if (!classified || !classified.assignment
          || line.positionDescription !== classified.line.positionDescription
          || line.totalPrice !== classified.line.totalPrice) return line;
        const { assignment } = classified;
        return {
          ...line,
          commodityGroupId: assignment.commodityGroupId ?? null,
          classificationSource: assignment.status === 'assigned' ? 'llm' : 'manual',
        };
      }));
      if (assignments.some((item) => item.status !== 'assigned')) {
        setWarning('Some item categories still need manual review.');
      }
    } catch {
      setWarning('Category assignment is unavailable. Select categories manually and try again later.');
    } finally {
      setClassifyingCategories(false);
    }
  };

  const updateAdjustment = (id: string, change: Partial<Adjustment>) => {
    setAdjustments((current) => current.map((item) => item.id === id ? { ...item, ...change } : item));
  };

  const removeAdjustment = (id: string) => {
    setAdjustments((current) => current.filter((item) => item.id !== id));
  };

  const serializedAdjustments: api.AdjustmentInput[] = adjustments.map(({ id: _id, ...item }) => ({
    kind: item.kind,
    description: item.description,
    amount: Number(item.amount) || 0,
    scope: item.scope,
    lineIndex: item.scope === 'line' ? item.lineIndex : null,
  }));

  const validateForm = () => {
    const next: FormErrors = {};
    if (!requestorName.trim()) next.requestorName = 'Requestor name is required.';
    if (!title.trim()) next.title = 'Title is required.';
    if (hasUnmatchedLines && !vendorName.trim()) next.vendorName = 'Vendor name is required for items kept from the quote.';
    if (hasUnmatchedLines && requiresVatId && !vatId.trim()) next.vatId = 'Tax ID is required for items kept from the quote.';
    if (requiresDepartment && !department.trim()) next.department = 'Department is required.';
    if (!orderLines.length) next.orderLines = 'Add at least one order line.';
    orderLines.forEach((line, index) => {
      if (!line.positionDescription.trim()) next[`line-${index}-description`] = 'Description is required.';
      if (!line.unit.trim()) next[`line-${index}-unit`] = 'Unit is required.';
      if (!/^[A-Za-z]{3}$/.test(line.currency)) next[`line-${index}-currency`] = 'Enter a 3-letter currency code.';
      if (!line.commodityGroupId) next[`line-${index}-category`] = 'Select or classify a category.';
    });
    if (quotedTotal < 0) next.quotedTotal = 'Quoted total cannot be negative.';
    if (hasTotalMismatch && !discrepancyNote.trim()) {
      next.discrepancyNote = 'Add a note or correct the quoted total before creating this request.';
    }
    setFormErrors(next);
    return Object.keys(next).length === 0;
  };

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (submitting) return;
    setError('');
    setSuccess('');
    setWarning('');
    if (!validateForm()) {
      setError('Review the highlighted fields before creating the request.');
      window.scrollTo({ top: 0, behavior: 'smooth' });
      return;
    }

    setSubmitting(true);
    try {
      const created = await api.requests.createBatch({
        requestorName,
        titleShortDescription: title,
        vendorName,
        vatId,
        totalCost: quotedTotal,
        hasExtractedQuote,
        sourceQuoteCurrency: quoteCurrency,
        department,
        orderLines: orderLines.map((line) => ({
          positionDescription: line.positionDescription,
          unitPrice: line.unitPrice,
          amount: line.amount,
          unit: line.unit,
          totalPrice: line.totalPrice,
          currency: line.currency,
          catalogArticleId: line.catalogArticleId,
          catalogArticleNumber: line.catalogArticleNumber,
          commodityGroupId: line.commodityGroupId,
          classificationSource: line.classificationSource || 'manual',
        })),
        financialBreakdown: { adjustments: serializedAdjustments },
        discrepancyNote: discrepancyNote.trim() || null,
      });

      if (file) {
        const documentResults = await Promise.allSettled(
          created.map((request) => api.requests.uploadDocument(request.id, file))
        );
        if (documentResults.some((result) => result.status === 'rejected')) {
          setWarning('The requests were created, but the original PDF could not be attached to every request.');
        }
      }

      setSuccess(created.length === 1
        ? 'Request created successfully. Redirecting...'
        : `${created.length} requests created successfully, one for each supplier and currency. Redirecting...`);
      window.scrollTo({ top: 0, behavior: 'smooth' });
      setTimeout(() => {
        router.push('/dashboard/requests');
        router.refresh();
      }, 1200);
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : 'Unable to create this request.';
      setError(message);
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } finally {
      setSubmitting(false);
    }
  };

  const adjustmentEditor = (item: Adjustment) => (
    <div key={item.id} className="grid grid-cols-1 sm:grid-cols-[130px_1fr_150px_auto] gap-3 rounded-lg border border-line bg-white p-3">
      <select
        value={item.kind}
        onChange={(event) => updateAdjustment(item.id, { kind: event.target.value as Adjustment['kind'] })}
        className="h-11 rounded-xl border border-line bg-white px-3 text-sm text-ink"
        aria-label="Adjustment type"
      >
        <option value="tax">Tax</option>
        <option value="discount">Discount</option>
        <option value="charge">Additional charge</option>
      </select>
      <Input aria-label="Adjustment description" value={item.description} onChange={(event) => updateAdjustment(item.id, { description: event.target.value })} placeholder="Sales tax, shipping, promotion, etc." />
      <Input aria-label="Adjustment amount" type="number" min="0" step="0.01" value={item.amount} onChange={(event) => updateAdjustment(item.id, { amount: asNumber(event.target.value) })} placeholder="0.00" />
      <Button type="button" variant="outline" size="sm" onClick={() => removeAdjustment(item.id)}>Remove</Button>
    </div>
  );

  return (
    <div className="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
      <div className="mb-8">
        <h1 className="text-3xl font-semibold tracking-tight text-ink">New Procurement Request</h1>
        <p className="text-gray-600 mt-2">Upload a vendor offer PDF or enter the request details manually.</p>
      </div>

      {error && <Alert variant="error" className="mb-6">{error}</Alert>}
      {warning && <Alert variant="warning" className="mb-6">{warning}</Alert>}
      {success && <Alert variant="success" className="mb-6">{success}</Alert>}

      <form onSubmit={handleSubmit} className="space-y-8">
        <div className="lio-card p-6">
          <h2 className="text-xl font-semibold tracking-tight text-ink mb-2">Upload Vendor Offer</h2>
          <p className="text-sm text-gray-600 mb-4">The extracted values remain editable. Please review them before submitting.</p>
          <div className="border-2 border-dashed border-gray-300 rounded-lg p-8 text-center hover:border-accent-deep/50 transition-colors">
            <input type="file" accept="application/pdf" onChange={handleFileChange} className="hidden" id="pdf-upload" disabled={extracting} />
            <label htmlFor="pdf-upload" className="cursor-pointer">
              {extracting ? <div className="flex flex-col items-center"><Loader size="lg" /><p className="mt-4 text-sm text-gray-600">{classifyingCategories ? 'Assigning item categories...' : 'Extracting data from PDF...'}</p></div> : <div><p className="text-sm font-medium text-gray-900">{file ? file.name : 'Click to upload a PDF'}</p><p className="mt-1 text-xs text-gray-500">PDF up to 10MB</p></div>}
            </label>
          </div>
        </div>

        <div className="lio-card p-6 space-y-4">
          <h2 className="text-xl font-semibold tracking-tight text-ink">Request Details</h2>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <Input label="Requestor Name *" value={requestorName} error={formErrors.requestorName} onChange={(event) => { setRequestorName(event.target.value); clearFieldError('requestorName'); }} placeholder="John Doe" />
            <Input label={requiresDepartment ? 'Department *' : 'Department'} value={department} error={formErrors.department} onChange={(event) => { setDepartment(event.target.value); clearFieldError('department'); }} placeholder="IT Department" />
          </div>
          <Input label="Title / Short Description *" value={title} error={formErrors.title} onChange={(event) => { setTitle(event.target.value); clearFieldError('title'); }} placeholder="Adobe Creative Cloud Subscription" />
        </div>

        <div className="lio-card p-6 space-y-4">
          <h2 className="text-xl font-semibold tracking-tight text-ink">Vendor Information</h2>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <Input label={hasUnmatchedLines ? 'Original Vendor Name *' : 'Original Vendor Name'} value={vendorName} error={formErrors.vendorName} onChange={(event) => { setVendorName(event.target.value); clearFieldError('vendorName'); }} placeholder="Vendor from quote" />
            <Input label={hasUnmatchedLines && requiresVatId ? 'Original Vendor Tax ID *' : 'Original Vendor Tax ID'} value={vatId} error={formErrors.vatId} onChange={(event) => { setVatId(event.target.value); clearFieldError('vatId'); }} placeholder="12-3456789" />
          </div>
          <p className="text-sm text-ink/55">Each item gets its own category. The request’s primary category is based on the highest-value category among its items.</p>
        </div>

        <div className="lio-card p-6">
          <div className="flex items-center justify-between mb-4"><div><h2 className="text-xl font-semibold tracking-tight text-ink">Order Lines *</h2><p className="text-sm text-ink/55 mt-1">Enter item totals and review each item category.</p></div><div className="flex gap-2"><Button type="button" onClick={classifyAllLines} variant="outline" size="sm" disabled={classifyingCategories || !orderLines.length}>{classifyingCategories ? 'Classifying…' : 'Classify items'}</Button><Button type="button" onClick={addOrderLine} variant="outline" size="sm">+ Add Line</Button></div></div>
          {formErrors.orderLines && <Alert variant="error" className="mb-4">{formErrors.orderLines}</Alert>}
          {orderLines.length === 0 ? <div className="text-center py-8 text-gray-500">No order lines yet. Add one to get started.</div> : <div className="space-y-4">
            {orderLines.map((line, index) => (
              <div key={line.localId} className="border border-gray-200 rounded-lg p-4">
                <div className="flex items-start justify-between mb-4"><h3 className="text-sm font-medium text-gray-700">Line {index + 1}</h3><button type="button" onClick={() => removeOrderLine(index)} className="text-red-600 hover:text-red-700 text-sm">Remove</button></div>
                <CatalogSuggestions
                  description={line.positionDescription}
                  selectedArticle={line.selectedArticle}
                  onSelect={(article) => selectCatalogArticle(index, article)}
                  onClearSelection={() => clearCatalogArticle(index)}
                />
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-6 gap-4">
                  <div className="lg:col-span-2">
                    <Input label="Description *" value={line.positionDescription} error={formErrors[`line-${index}-description`]} disabled={!!line.selectedArticle} onChange={(event) => { updateOrderLine(index, 'positionDescription', event.target.value); clearFieldError(`line-${index}-description`); }} placeholder="Item description" />
                  </div>
                  <Input label={`Unit Price (${line.currency})`} type="number" min="0" step="0.01" value={line.unitPrice} disabled={!!line.selectedArticle} onChange={(event) => updateOrderLine(index, 'unitPrice', asNumber(event.target.value))} />
                  <Input label="Amount" type="number" min="0" step="0.0001" value={line.amount} onChange={(event) => updateOrderLine(index, 'amount', asNumber(event.target.value))} />
                  <Input label="Unit *" value={line.unit} error={formErrors[`line-${index}-unit`]} disabled={!!line.selectedArticle} onChange={(event) => { updateOrderLine(index, 'unit', event.target.value); clearFieldError(`line-${index}-unit`); }} placeholder="pieces, licenses" />
                  <Input label="Currency" value={line.currency} error={formErrors[`line-${index}-currency`]} disabled={!!line.selectedArticle} maxLength={3} onChange={(event) => updateOrderLine(index, 'currency', event.target.value)} placeholder="USD" />
                </div>
                <div className="mt-3 max-w-sm">
                  <Input label={`Item Total (${line.currency})`} type="number" min="0" step="0.01" value={line.totalPrice} onChange={(event) => updateOrderLine(index, 'totalPrice', asNumber(event.target.value))} helperText="Update this if the quoted line total differs from unit price × amount." />
                </div>
                <div className="mt-3">
                  <label className="block text-sm font-medium text-gray-700 mb-1.5">Item Category *</label>
                  <select
                    value={line.commodityGroupId || ''}
                    onChange={(event) => {
                      invalidateLineClassification(line.localId);
                      const groupId = Number(event.target.value) || null;
                      setOrderLines((current) => current.map((currentLine, lineIndex) => lineIndex === index
                        ? { ...currentLine, commodityGroupId: groupId, classificationSource: 'manual' }
                        : currentLine));
                      clearFieldError(`line-${index}-category`);
                    }}
                    className={`w-full px-3 py-2 border rounded-lg text-gray-900 ${formErrors[`line-${index}-category`] ? 'border-red-500' : 'border-gray-300'}`}
                  >
                    <option value="">{commodityGroups ? 'Select category' : 'Loading categories...'}</option>
                    {commodityGroups?.map((group) => <option key={group.id} value={group.id}>{group.category} · {group.name}</option>)}
                  </select>
                  <p className="mt-1 text-xs text-gray-500">
                    {formErrors[`line-${index}-category`] || (classifyingLineIds.has(line.localId) ? 'Updating category for selected catalog item…' : line.classificationSource === 'llm' ? 'Suggested by AI — review before saving.' : line.commodityGroupId ? 'Selected manually.' : 'Needs review.')}
                  </p>
                </div>
              </div>
            ))}
          </div>}
        </div>

        {(hasCatalogSelection || hasMultipleGroups) && (
          <div className="lio-card border border-amber-200 bg-amber-50/70 p-6">
            <h2 className="text-lg font-semibold tracking-tight text-ink">Requests to create</h2>
            <p className="mt-1 text-sm text-ink/70">
              {requestGroups.length > 1
                ? `Items from different suppliers or currencies will create ${requestGroups.length} separate requests.`
                : 'The selected catalog supplier and negotiated prices will be used for this request.'}
            </p>
            <ul className="mt-3 space-y-1 text-sm text-ink/80">
              {requestGroups.map((group) => (
                <li key={group.key}>
                  {group.supplierName || 'Original supplier'} · {group.count} item{group.count === 1 ? '' : 's'} · subtotal {formatMoney(group.subtotal, group.currency)} · request total {formatMoney(group.total, group.currency)}
                </li>
              ))}
            </ul>
          </div>
        )}

        <details className="lio-card p-6 group">
          <summary className="cursor-pointer list-none flex items-center justify-between"><div><h2 className="text-xl font-semibold tracking-tight text-ink">Taxes, Discounts, and Additional Charges</h2><p className="text-sm text-ink/55 mt-1">Hidden until you choose to review or edit them.</p></div><span className="text-sm text-accent-deep group-open:hidden">Show</span><span className="text-sm text-accent-deep hidden group-open:inline">Hide</span></summary>
          <div className="mt-5 space-y-5">
            <div className="space-y-3"><p className="text-sm font-medium text-ink">Quote-level adjustments</p>{adjustments.filter((item) => item.scope === 'order').map(adjustmentEditor)}<Button type="button" variant="outline" size="sm" onClick={() => setAdjustments((current) => [...current, newAdjustment('order')])}>+ Add quote-level adjustment</Button></div>
            {orderLines.map((line, index) => <div key={`line-adjustments-${index}`} className="border-t border-line pt-4 space-y-3"><div className="flex items-center justify-between"><p className="text-sm font-medium text-ink">Line {index + 1}: {line.positionDescription || 'Untitled item'}</p><Button type="button" variant="outline" size="sm" onClick={() => setAdjustments((current) => [...current, newAdjustment('line', index)])}>+ Add line adjustment</Button></div>{adjustments.filter((item) => item.scope === 'line' && item.lineIndex === index).map(adjustmentEditor)}</div>)}
          </div>
        </details>

        <div className="bg-accent-soft/20 border-2 border-accent/40 rounded-lg p-6 space-y-4">
          {hasCatalogSelection && <Alert variant="warning">Catalog prices changed some item amounts. Tax may depend on which items were bought, so review the tax and final request total.</Alert>}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6 items-end">
            <Input label={`Quoted Total (${quoteCurrency})`} type="number" min="0" step="0.01" value={quotedTotal} error={formErrors.quotedTotal} onChange={(event) => { setQuotedTotal(asNumber(event.target.value)); clearFieldError('quotedTotal'); }} helperText="The final total stated in the original vendor quote." />
            <div className="pb-1"><p className="text-sm font-medium text-ink/70">{hasMultipleGroups ? 'Request totals' : hasExtractedQuote && canCompareQuoteTotal ? 'Calculated Quote Total' : 'Calculated Request Total'}</p><p className="mt-1 text-2xl font-bold text-accent-deep">{hasMultipleGroups ? 'See supplier totals above' : formatMoney(requestGroups[0]?.total ?? calculation.total, requestGroups[0]?.currency ?? orderLines[0]?.currency ?? 'USD')}</p></div>
          </div>
          <p className="text-sm text-ink/65">{canCompareQuoteTotal ? 'Calculated from item totals and quote-level taxes, discounts, and charges.' : 'Each request is totaled using its own supplier, currency, item totals, and applicable adjustments.'}</p>
          {hasTotalMismatch ? <div className="rounded-lg bg-red-50 border border-red-200 p-4"><p className="text-sm font-medium text-red-700">The quoted total ({formatMoney(quotedTotal, quoteCurrency)}) does not match the calculated total ({formatMoney(calculation.total, orderLines[0]?.currency || 'USD')}).</p><TextArea label="Discrepancy note *" value={discrepancyNote} error={formErrors.discrepancyNote} onChange={(event) => { setDiscrepancyNote(event.target.value); clearFieldError('discrepancyNote'); }} placeholder="Explain why the quote total differs, or correct the quoted total above." className="mt-3" /></div> : canCompareQuoteTotal ? <p className="text-sm text-green-700">Quoted total matches the selected items, taxes, and discounts.</p> : hasExtractedQuote ? <p className="text-sm text-ink/65">The original quote total is kept for reference. Catalog selections are priced at the negotiated catalog price.</p> : <p className="text-sm text-ink/65">The request total is calculated from the item totals and adjustments above.</p>}
        </div>

        <div className="flex items-center justify-end space-x-4"><Button type="button" variant="outline" onClick={() => router.back()}>Cancel</Button><Button type="submit" size="lg" disabled={submitting || extracting}>{submitting ? 'Creating…' : requestGroups.length > 1 ? `Create ${requestGroups.length} Requests` : 'Create Request'}</Button></div>
      </form>
    </div>
  );
}
