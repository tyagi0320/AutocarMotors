"""Deterministic checks for the customer chat and paid search tool."""
import re

SCOPE_REPLY = "I can help with cars, dealership services, and showroom bookings. What would you like to know about AutoCare Motors?"

BOOKING_INTENT = re.compile(
    r"\b(book|reserve|reschedule|cancel|schedule a visit|make an appointment|move it|come in|come by|"
    r"(?:like to|want to|can i) visit|visit the showroom|visit (?:tomorrow|today|on|at|[a-z]+day)|"
    r"see the .+ (?:on|at|tomorrow)|test drive)\b",
    re.I,
)
CONTACT_REQUEST = re.compile(
    r"\b(?:your|full)\s+(?:name|e-?mail|phone|contact details)\b|"
    r"\b(?:name and e-?mail|phone number)\b|"
    r"\bwhat name\b|"
    r"\b(?:provide|share|send|give|need|have)\b[^.!?\n]{0,32}\b(?:e-?mail address|booking id|contact details)\b",
    re.I,
)
MANAGE_BOOKING = re.compile(r"\b(find|look up|reschedule|cancel|move|change)\b.{0,30}\b(booking|visit|appointment|it)\b", re.I)
BOOKING_REFERENCE = re.compile(r"\b[\da-f]{8}(?:-[\da-f]{4}){3}-[\da-f]{12}\b", re.I)
GREETING = re.compile(r"(?:hi|hello|hey|good morning|good afternoon|good evening)[!.\s]*", re.I)
VISIT_DETAILS = re.compile(
    r"\b(?:[a-z]{3,9}day|tomorrow|today|\d{1,2}:\d{2}|\d{1,2}\s*[ap]\.?m\.?|"
    r"at\s+\d{1,2}|\d{4}-\d\d-\d\d)\b", re.I,
)
VISIT_OFFER = re.compile(
    r"\b(?:would you like|do you want|shall i|should i|can i help you)\b[^?.!]*"
    r"\b(?:book|reserve|schedule|arrange|set up)\b[^?.!]*\b(?:visit|appointment|test drive|slot)\b", re.I,
)
ACCEPT = re.compile(
    r"(?:yes|yeah|yep|sure|okay|ok|please do|sounds good|that works|go ahead|"
    r"yes[, ]+please|yeah[, ]+sure|sure thing|yes[, ]+i['’]d like to)[.! ,]*", re.I,
)
DECLINE = re.compile(r"(?:no|no thanks|not now|never mind|nevermind|don't book)[.! ,]*", re.I)
NAME_REPLY = re.compile(r"(?:(?:my )?name is |i am |i['’]m )?[^\W\d_]+(?:[ '’-][^\W\d_]+){0,4}[.!]*", re.I)
VISIT_FINISHED = re.compile(r"^(?:Your showroom visit (?:was (?:rescheduled|cancelled)|for .+ is booked)|I discarded the )")
DISCOUNT_REQUEST = re.compile(r"\b(?:discounts?|special price|lower (?:interest )?rate|price negotiable)\b", re.I)
FINANCING_REQUEST = re.compile(r"\b(?:emi|loan|financ(?:e|ing)|installments?)\b", re.I)
DISCOUNT_REPLY = "Please visit the showroom in person to discuss discounts or pricing offers."
