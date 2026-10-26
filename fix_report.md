# Fix Report

This report summarizes the changes made for the four case requirements. It describes what users can do now, the decisions made, and any remaining gaps.

## 1. PDF extraction reliability

### 1. Problem Statement

Quote uploads could fail completely when a field, such as a tax ID, was missing. Extracted information could also be incomplete or inaccurate: alternatives could be treated as items to buy, taxes and discounts were not visible, and there was no reliable check that the quoted total matched the selected items.

### 2. Functional Requirements

- Keep an extraction usable when some fields are missing, and ask the user to complete those fields before saving.
- Show clear, non-technical messages when an upload or extraction cannot be completed.
- Exclude alternatives from the items being bought.
- Show taxes and discounts without cluttering the main request form.
- Check that item values, taxes, discounts, and charges agree with the quote total.
- Let users add, edit, or remove items before creating the request, while retaining the original PDF.

### 3. Implemented Solution

The extraction response now accepts blank or missing values instead of rejecting the complete quote. It returns the available information with a list of required fields that still need attention. The request form displays those fields for the user to complete and uses business-friendly error messages if extraction is unavailable or unreadable.

Clearly labelled alternative and optional lines are removed from the selected items. Delivery or installation lines are recorded as quote-level charges. Taxes, discounts, and charges are stored as adjustments and shown in an expandable **Taxes, discounts, and charges** section. The application calculates a total from the selected line totals plus quote-level taxes and charges, less quote-level discounts. This calculation is repeated by the server when the request is saved. If it does not match the printed quote total, the request cannot be saved until the user corrects the values or supplies an explanation. The PDF is uploaded with the request and remains available for review.

### 4. Current Behavior

After upload, users see an editable request form rather than an all-or-nothing result. They can complete missing fields, change or delete extracted items, and add missing items. The form shows the quote total and calculated total separately. A difference is clearly flagged and must be resolved or explained before the request is created.

### 5. Assumptions and Decisions Made

- The printed vendor total remains the quote total shown to the user.
- A line total is treated as already including any tax or discount that belongs only to that line.
- Quote-level taxes and charges are added, while quote-level discounts are subtracted, when checking the overall total.
- An alternative is excluded when its description is clearly marked as alternative or optional.

### 6. Current Limitations and Issues

- Image-only or poor-quality scanned PDFs may not contain readable text and require manual entry.
- Alternative detection depends on clear wording, so an unusually labelled alternative may still need manual removal.
- The application checks the overall total but does not independently verify every quantity multiplied by unit price against the line total.
- The five supplied PDFs are not maintained as a fixed expected-results regression set, so extraction quality can still vary with new layouts or AI responses.

## 2. Negotiated catalog prices

### 1. Problem Statement

Requesters could enter any product and price without being shown their organization’s negotiated catalog options. This made it easy to miss approved suppliers and agreed prices.

### 2. Functional Requirements

- Check the current organization’s catalog for every request item.
- Show one or more matching catalog options for an item.
- Let the requester select a catalog item or keep the entered item.
- Provide a catalog search for each item.
- Warn users when selected items use different suppliers and create separate requests by supplier.

### 3. Implemented Solution

For each item, the application searches only the signed-in organization’s catalog. Matching ranks exact article-number matches, exact description matches, and keyword matches. Each suggestion shows the supplier, article number, negotiated price, currency, unit, and the reason it matched.

Choosing a suggestion replaces the item’s relevant details with catalog data and records the chosen catalog article. Users can remove that choice or search again for a specific item. When saving, the server confirms that each selected article belongs to the organization and uses its current supplier, price, currency, and unit. It then creates one request for each supplier and currency combination, rather than combining incompatible purchases.

### 4. Current Behavior

Catalog suggestions appear with each request item. A user can choose a suggestion, search the catalog, or keep the original item. If selections result in more than one supplier, the form shows a warning and preview; creating the request produces separate requests for those suppliers.

### 5. Assumptions and Decisions Made

- Catalog use is optional; a requester can create a non-catalog request.
- Different currencies are not combined into a single request because their totals cannot be compared safely.
- An item without a catalog selection stays with the supplier on the original quote.

### 6. Current Limitations and Issues

- Matching is based on article numbers, normalized descriptions, and keywords rather than broad semantic matching. A good match can be missed, so users may need to search or retain their entered item.
- When a quote is split across suppliers, quote-level taxes, discounts, and charges can only be kept with the original quote supplier. If that allocation is unclear, the request is rejected until the user corrects it.

## 3. Custom commodity-group rules

### 1. Problem Statement

The standard category suggestion did not always match Acme’s finance rules. For example, Acme needs cable ties booked to Electrical Engineering, printer toner to IT Hardware, and small software subscriptions to Office Supplies.

### 2. Functional Requirements

- Apply Acme’s special rules to individual items.
- Keep each organization’s rules separate.
- Allow an administrator to view and update the rules.
- Ensure a matching custom rule takes priority over the general classification.
- Record when a custom rule changes the category.

### 3. Implemented Solution

Acme is seeded with rules for cable ties and zip ties, printer toner, and small software subscriptions. Rules are stored in organization settings and point to valid saved commodity groups. Administrators can view and edit the rules in organization settings.

When classifying an item, the category service sends the current organization’s rules and its valid category list to the AI. The chosen category is saved per item, so mixed-item requests can retain different accounting categories. Rules from other organizations are not included in the classification request, and uncertain results are marked for manual review.

### 4. Current Behavior

For Acme, the category suggestion considers its custom rules for each item. Users can review and change the suggested category. Other organizations do not receive Acme’s rules.

### 5. Assumptions and Decisions Made

- Rules are organization-specific and must reference an existing commodity group.
- “Small” software subscriptions are not assigned a numerical threshold; the rule wording is interpreted during classification unless an administrator makes it more specific.
- A request may contain several categories because categories are saved at item level.

### 6. Current Limitations and Issues

- This is not yet a deterministic override. The rules guide the AI, so a matching item can still receive a different category or be marked for review.
- The system does not write a separate audit-history entry when a rule changes a category.

These two points mean the requirement that a matching rule must always win, and that every override must be logged, is only partially satisfied.

## 4. Approval threshold

### 1. Problem Statement

Every request entered the buyer approval queue, including small requests that should be ready to proceed immediately.

### 2. Functional Requirements

- Skip buyer approval for requests below $2,000.
- Use the full request value, including applicable tax and delivery.
- Keep a request of exactly $2,000 in the approval process.
- Reassess approval when financial details are changed.
- Record why approval was skipped.

### 3. Implemented Solution

The server evaluates the request total when a request is created and whenever financial details are edited. A USD request below $2,000 is assigned **Approval not required**. The system saves a note explaining the threshold decision and creates an activity-history entry. At $2,000 or above, the request remains pending for buyer or administrator approval. A financial change that crosses the threshold clears the earlier decision and applies the appropriate status again.

### 4. Current Behavior

New USD requests below $2,000 do not appear as pending buyer work. Requests at $2,000 or more do. Updating a request’s financial details can cause it to enter or leave the approval process, and the change is recorded in its history.

### 5. Assumptions and Decisions Made

- “Below $2,000” means $1,999.99 or less; exactly $2,000 requires approval.
- The test uses the request’s quoted total, including quote-level tax and delivery where present.
- The automatic threshold applies only to USD amounts.

### 6. Current Limitations and Issues

- Non-USD requests remain pending because the application does not convert currencies before comparing the amount to the USD threshold.
- Existing requests that were pending before this change are not automatically reclassified; they are reassessed when their financial details are edited.
