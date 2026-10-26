'use client';

import { useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useMutation } from '@tanstack/react-query';
import * as api from '@/lib/api';
import { useCommodityGroups } from '@/lib/hooks/useCommodityGroups';
import { Alert, Button, Input, Loader, TextArea } from '@/components/base';

interface OrderLine extends api.OrderLineInput {}

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
  const [commodityGroupId, setCommodityGroupId] = useState<number | null>(null);
  const [orderLines, setOrderLines] = useState<OrderLine[]>([]);
  const [adjustments, setAdjustments] = useState<Adjustment[]>([]);
  // Kept separate from the calculated total so that quote mismatches are visible.
  const [quotedTotal, setQuotedTotal] = useState(0);
  const [discrepancyNote, setDiscrepancyNote] = useState('');
  const [requiredFields, setRequiredFields] = useState<api.ConfigurableRequiredField[]>([]);

  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [warning, setWarning] = useState('');
  const [formErrors, setFormErrors] = useState<FormErrors>({});
  const [extracting, setExtracting] = useState(false);

  useEffect(() => {
    api.organizations.getSettings()
      .then((settings) => setRequiredFields(settings.required_fields))
      .catch(() => undefined);
  }, []);

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

  const hasTotalMismatch = Math.abs(quotedTotal - calculation.total) > 0.005;
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
      if (missingFields.includes('Commodity Group')) next.commodityGroupId = 'Commodity group is required.';
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
      setOrderLines((extracted.orderLines || []).map((line) => ({
        positionDescription: line.positionDescription || '',
        unitPrice: Number(line.unitPrice) || 0,
        amount: Number(line.amount) || 0,
        unit: line.unit || '',
        totalPrice: Number(line.totalPrice) || 0,
      })));
      setAdjustments((extracted.adjustments || []).map(normalizeAdjustment));
      setQuotedTotal(Number(extracted.totalCost) || 0);
      setDiscrepancyNote('');
      if (extracted.commodityGroupId) setCommodityGroupId(extracted.commodityGroupId);

      applyExtractionWarnings(extractionResult.missingFields);
      setSuccess('Quote extracted. Review the fields, items, taxes, and discounts before creating the request.');
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
      { positionDescription: '', unitPrice: 0, amount: 1, unit: '', totalPrice: 0 },
    ]);
    clearFieldError('orderLines');
  };

  const updateOrderLine = (index: number, field: keyof OrderLine, value: string | number) => {
    setOrderLines((current) => current.map((line, lineIndex) => {
      if (lineIndex !== index) return line;
      const updated = { ...line, [field]: value };
      if (field === 'unitPrice' || field === 'amount') {
        updated.totalPrice = roundCurrency(Number(updated.unitPrice) * Number(updated.amount));
      }
      return updated;
    }));
    clearFieldError('orderLines');
  };

  const removeOrderLine = (index: number) => {
    setOrderLines((current) => current.filter((_, lineIndex) => lineIndex !== index));
    setAdjustments((current) => current
      .filter((item) => item.scope !== 'line' || item.lineIndex !== index)
      .map((item) => (
        item.scope === 'line' && item.lineIndex !== null && item.lineIndex > index
          ? { ...item, lineIndex: item.lineIndex - 1 }
          : item
      )));
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
    if (!vendorName.trim()) next.vendorName = 'Vendor name is required.';
    if (requiresVatId && !vatId.trim()) next.vatId = 'Tax ID is required.';
    if (requiresDepartment && !department.trim()) next.department = 'Department is required.';
    if (!commodityGroupId) next.commodityGroupId = 'Commodity group is required.';
    if (!orderLines.length) next.orderLines = 'Add at least one order line.';
    orderLines.forEach((line, index) => {
      if (!line.positionDescription.trim()) next[`line-${index}-description`] = 'Description is required.';
      if (!line.unit.trim()) next[`line-${index}-unit`] = 'Unit is required.';
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
    setError('');
    setSuccess('');
    setWarning('');
    if (!validateForm()) {
      setError('Review the highlighted fields before creating the request.');
      window.scrollTo({ top: 0, behavior: 'smooth' });
      return;
    }

    try {
      const created = await api.requests.create({
        requestorName,
        titleShortDescription: title,
        vendorName,
        vatId,
        commodityGroupId: commodityGroupId!,
        totalCost: quotedTotal,
        department,
        orderLines,
        financialBreakdown: { adjustments: serializedAdjustments },
        discrepancyNote: discrepancyNote.trim() || null,
      });

      if (file) {
        try {
          await api.requests.uploadDocument(created.id, file);
        } catch {
          setWarning('The request was created, but the original PDF could not be attached.');
        }
      }

      setSuccess('Request created successfully. Redirecting...');
      window.scrollTo({ top: 0, behavior: 'smooth' });
      setTimeout(() => {
        router.push('/dashboard/requests');
        router.refresh();
      }, 1200);
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : 'Unable to create this request.';
      setError(message);
      window.scrollTo({ top: 0, behavior: 'smooth' });
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
              {extracting ? <div className="flex flex-col items-center"><Loader size="lg" /><p className="mt-4 text-sm text-gray-600">Extracting data from PDF...</p></div> : <div><p className="text-sm font-medium text-gray-900">{file ? file.name : 'Click to upload a PDF'}</p><p className="mt-1 text-xs text-gray-500">PDF up to 10MB</p></div>}
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
            <Input label="Vendor Name *" value={vendorName} error={formErrors.vendorName} onChange={(event) => { setVendorName(event.target.value); clearFieldError('vendorName'); }} placeholder="Adobe Systems" />
            <Input label={requiresVatId ? 'Tax ID *' : 'Tax ID'} value={vatId} error={formErrors.vatId} onChange={(event) => { setVatId(event.target.value); clearFieldError('vatId'); }} placeholder="12-3456789" />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">Commodity Group *</label>
            <select value={commodityGroupId || ''} onChange={(event) => { setCommodityGroupId(Number(event.target.value) || null); clearFieldError('commodityGroupId'); }} className={`w-full px-3 py-2 border rounded-lg text-gray-900 focus:ring-2 focus:ring-accent-deep/60 ${formErrors.commodityGroupId ? 'border-red-500' : 'border-gray-300'}`}>
              <option value="">{commodityGroups ? 'Select a commodity group' : 'Loading...'}</option>
              {commodityGroups?.map((group) => <option key={group.id} value={group.id}>{group.id} - {group.category} - {group.name}</option>)}
            </select>
            {formErrors.commodityGroupId && <p className="mt-1.5 text-sm text-red-600">{formErrors.commodityGroupId}</p>}
          </div>
        </div>

        <div className="lio-card p-6">
          <div className="flex items-center justify-between mb-4"><div><h2 className="text-xl font-semibold tracking-tight text-ink">Order Lines *</h2><p className="text-sm text-ink/55 mt-1">Enter item totals before taxes and discounts.</p></div><Button type="button" onClick={addOrderLine} variant="outline" size="sm">+ Add Line</Button></div>
          {formErrors.orderLines && <Alert variant="error" className="mb-4">{formErrors.orderLines}</Alert>}
          {orderLines.length === 0 ? <div className="text-center py-8 text-gray-500">No order lines yet. Add one to get started.</div> : <div className="space-y-4">
            {orderLines.map((line, index) => (
              <div key={index} className="border border-gray-200 rounded-lg p-4">
                <div className="flex items-start justify-between mb-4"><h3 className="text-sm font-medium text-gray-700">Line {index + 1}</h3><button type="button" onClick={() => removeOrderLine(index)} className="text-red-600 hover:text-red-700 text-sm">Remove</button></div>
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-4">
                  <div className="lg:col-span-2"><Input label="Description *" value={line.positionDescription} error={formErrors[`line-${index}-description`]} onChange={(event) => { updateOrderLine(index, 'positionDescription', event.target.value); clearFieldError(`line-${index}-description`); }} placeholder="Item description" /></div>
                  <Input label="Unit Price ($)" type="number" min="0" step="0.01" value={line.unitPrice} onChange={(event) => updateOrderLine(index, 'unitPrice', asNumber(event.target.value))} />
                  <Input label="Amount" type="number" min="0" step="0.0001" value={line.amount} onChange={(event) => updateOrderLine(index, 'amount', asNumber(event.target.value))} />
                  <Input label="Unit *" value={line.unit} error={formErrors[`line-${index}-unit`]} onChange={(event) => { updateOrderLine(index, 'unit', event.target.value); clearFieldError(`line-${index}-unit`); }} placeholder="pieces, licenses" />
                </div>
                <div className="mt-3 flex items-center justify-between"><span className="text-sm text-gray-600">Quoted item total</span><span className="text-lg font-semibold tracking-tight text-ink">${line.totalPrice.toFixed(2)}</span></div>
              </div>
            ))}
          </div>}
        </div>

        <details className="lio-card p-6 group">
          <summary className="cursor-pointer list-none flex items-center justify-between"><div><h2 className="text-xl font-semibold tracking-tight text-ink">Taxes, Discounts, and Additional Charges</h2><p className="text-sm text-ink/55 mt-1">Hidden until you choose to review or edit them.</p></div><span className="text-sm text-accent-deep group-open:hidden">Show</span><span className="text-sm text-accent-deep hidden group-open:inline">Hide</span></summary>
          <div className="mt-5 space-y-5">
            <div className="space-y-3"><p className="text-sm font-medium text-ink">Quote-level adjustments</p>{adjustments.filter((item) => item.scope === 'order').map(adjustmentEditor)}<Button type="button" variant="outline" size="sm" onClick={() => setAdjustments((current) => [...current, newAdjustment('order')])}>+ Add quote-level adjustment</Button></div>
            {orderLines.map((line, index) => <div key={`line-adjustments-${index}`} className="border-t border-line pt-4 space-y-3"><div className="flex items-center justify-between"><p className="text-sm font-medium text-ink">Line {index + 1}: {line.positionDescription || 'Untitled item'}</p><Button type="button" variant="outline" size="sm" onClick={() => setAdjustments((current) => [...current, newAdjustment('line', index)])}>+ Add line adjustment</Button></div>{adjustments.filter((item) => item.scope === 'line' && item.lineIndex === index).map(adjustmentEditor)}</div>)}
          </div>
        </details>

        <div className="bg-accent-soft/20 border-2 border-accent/40 rounded-lg p-6 space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6 items-end"><Input label="Quoted Total ($) *" type="number" min="0" step="0.01" value={quotedTotal} error={formErrors.quotedTotal} onChange={(event) => { setQuotedTotal(asNumber(event.target.value)); clearFieldError('quotedTotal'); }} helperText="The final total stated in the vendor quote." /><div className="pb-1"><p className="text-sm font-medium text-ink/70">Calculated Total</p><p className="mt-1 text-3xl font-bold text-accent-deep">${calculation.total.toFixed(2)}</p></div></div>
          <p className="text-sm text-ink/65">Calculated from quoted item totals and quote-level adjustments. Item-level taxes and discounts are retained for review but do not affect this check.</p>
          {hasTotalMismatch ? <div className="rounded-lg bg-red-50 border border-red-200 p-4"><p className="text-sm font-medium text-red-700">The quoted total (${quotedTotal.toFixed(2)}) does not match the calculated total (${calculation.total.toFixed(2)}).</p><TextArea label="Discrepancy note *" value={discrepancyNote} error={formErrors.discrepancyNote} onChange={(event) => { setDiscrepancyNote(event.target.value); clearFieldError('discrepancyNote'); }} placeholder="Explain why the quote total differs, or correct the quoted total above." className="mt-3" /></div> : <p className="text-sm text-green-700">Quoted total matches the selected items, taxes, and discounts.</p>}
        </div>

        <div className="flex items-center justify-end space-x-4"><Button type="button" variant="outline" onClick={() => router.back()}>Cancel</Button><Button type="submit" size="lg">Create Request</Button></div>
      </form>
    </div>
  );
}
