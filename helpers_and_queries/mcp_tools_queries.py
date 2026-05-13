GET_TYPES_QUERY = """
    query getTypes {
          __type(name: "Query") {
            name
            fields {
              name
              type {
                name
                kind
                ofType {
                  name
                  kind
                }
              }
            }
          }
        }
    """


GET_TYPE_DETAILS_QUERY = """
    query GetTypeDetails($typeName: String!) {
  __type(name: $typeName) {
    name
    kind
    description
    fields {
      name
      description
      args {
        name
        description
        defaultValue
        type {
          name
          kind
          ofType {
            name
            kind
            ofType {
              name
              kind
            }
          }
        }
      }
      type {
        name
        kind
        ofType {
          name
          kind
          ofType {
            name
            kind
          }
        }
      }
    }
    inputFields {
      name
      description
      defaultValue
      type {
        name
        kind
        ofType {
          name
          kind
          ofType {
            name
            kind
          }
        }
      }
    }
    enumValues {
      name
      description
      isDeprecated
      deprecationReason
    }
    possibleTypes {
      name
      kind
    }
  }
}
    """


CREATE_JOURNAL_ENTRY_MUTATION = """
    mutation JournalEntryDraft_CreateMutation($input: CreateJournalEntryInput!) {
    createJournalEntry(input: $input) {
        ...JournalEntryFullFragment
        __typename
    }
    }

    fragment JournalEntryGridFragment on JournalEntry {
    id
    type
    name
    number
    amount
    vatAmount
    date
    description
    posted
    amountInclVat
    extra {
        amount
        transferType
        text
        provider
        __typename
    }
    reverts {
        ... on Reminder {
        id
        name
        number
        reminderFlow {
            id
            __typename
        }
        __typename
        }
        ... on Invoice {
        id
        name
        number
        isCreditNote
        __typename
        }
        ... on JournalEntry {
        id
        name
        number
        posted
        __typename
        }
        __typename
    }
    revertedBy {
        id
        name
        number
        posted
        __typename
    }
    __typename
    }

    fragment MetafieldDefinitionFragment on MetafieldDefinition {
    id
    ownerType
    key
    namespace
    name
    description
    type
    unitType
    list
    options
    hidden
    visibility
    position
    __typename
    }

    fragment MetafieldFragment on Metafield {
    id
    ownerType
    key
    namespace
    type
    list
    value
    definition {
        id
        __typename
    }
    __typename
    }

    fragment HasMetafieldsFragment on HasMetafields {
    metafieldDefinitions(sort: [{position: ASC}, {name: ASC}]) {
        ...MetafieldDefinitionFragment
        __typename
    }
    metafields {
        ...MetafieldFragment
        __typename
    }
    __typename
    }

    fragment LeaseAgreementPageEagerFragment on LeaseAgreement {
    id
    status
    canUseElectronicInvoicing
    startDate
    endDate
    current
    vacatingDate
    terminationRerentalFromDate
    occupationCancelledAt
    fullSerialNumber
    externalReference
    lease {
        id
        __typename
    }
    tenants(sort: [{name: ASC}]) {
        id
        name
        __typename
    }
    __typename
    }

    fragment LeasePageEagerFragment on Lease {
    id
    zip
    city
    rooms
    area
    category
    partialAddress
    fullAddress
    discontinuedReason
    childrenLeasesCount
    fullSerialNumber
    externalReference
    isCommercial
    property {
        id
        active
        __typename
    }
    parentLease {
        id
        partialAddress
        __typename
    }
    __typename
    }

    fragment LeaseReferenceFragment on Lease {
    ...LeasePageEagerFragment
    id
    typeEnum
    typeOther
    __typename
    }

    fragment DimensionableFragment on Dimensionable {
    ... on Lease {
        id
        zip
        city
        fullAddress
        partialAddress
        fullSerialNumber
        __typename
    }
    ... on Property {
        id
        name
        city
        zip
        fullSerialNumber
        __typename
    }
    ... on LeaseAgreement {
        ...LeaseAgreementPageEagerFragment
        lease {
        ...LeaseReferenceFragment
        __typename
        }
        __typename
    }
    __typename
    }

    fragment PropertyElement_PropertyFragment on Property {
    id
    name
    zip
    city
    fullSerialNumber
    __typename
    }

    fragment LeaseElement_LeaseFragment on Lease {
    id
    category
    fullAddress
    fullSerialNumber
    __typename
    }

    fragment LeaseAgreementElement_LeaseAgreementFragment on LeaseAgreement {
    id
    fullSerialNumber
    status
    tenants(sort: [{name: ASC}]) {
        id
        name
        __typename
    }
    lease {
        id
        partialAddress
        fullAddress
        __typename
    }
    parent {
        id
        __typename
    }
    childrenCount
    __typename
    }

    fragment DimensionableElement_DimensionFragment on Dimensionable {
    __typename
    ... on Property {
        ...PropertyElement_PropertyFragment
        __typename
    }
    ... on Lease {
        ...LeaseElement_LeaseFragment
        __typename
    }
    ... on LeaseAgreement {
        ...LeaseAgreementElement_LeaseAgreementFragment
        __typename
    }
    }

    fragment TransactionLineFragment on TransactionLine {
    id
    amount
    vatAmount
    amountInclVat
    description
    subdescription
    group
    order
    ledgerType
    hidden
    account {
        id
        name
        code
        __typename
    }
    contraAccount {
        id
        name
        code
        __typename
    }
    vat {
        id
        code
        name
        rate
        __typename
    }
    contraVat {
        id
        code
        name
        rate
        __typename
    }
    dimensionable {
        ...DimensionableFragment
        ...DimensionableElement_DimensionFragment
        __typename
    }
    __typename
    }

    fragment GroupKeyValueFiles_FileFragment on File {
    id
    url
    name
    __typename
    }

    fragment JournalEntryFullFragment on JournalEntry {
    ...JournalEntryGridFragment
    ...HasMetafieldsFragment
    date
    createdAt
    canBeReverted
    revertedBy {
        id
        name
        __typename
    }
    transactionLines {
        ...TransactionLineFragment
        __typename
    }
    files {
        ...GroupKeyValueFiles_FileFragment
        __typename
    }
    __typename
    }
    """




CREATE_FILE_MUTATION = """
    mutation CreateFile($input: CreateFileInput!) {
        createFile(input: $input) {
            id
            name
            mime
            type
        }
    }
    """

GET_TENANTS_QUERY = """
    query getTenants {
      tenants(first: 50, page: 1) {
        data {
          id
          name
          email
        }
      }
    }
    """



# CREATE_ORPHANED_FILE_MUTATION = """
#     mutation CreateOrphanedFile($input: CreateOrphanedFileInput!) {
#       createOrphanedFile(input: $input) {
#         id
#         filename
#         mime_type
#         size
#         url
#         created_at
#       }
#     }
#     """

GET_INPUT_TYPE_QUERY = """
    query GetInputType($typeName: String!) {
      __type(name: $typeName) {
        name
        kind
        inputFields {
          name
          description
          type {
            name
            kind
            ofType {
              name
              kind
            }
          }
        }
      }
    }
    """




#TODO verificer queries

GET_ACCOUNTS_QUERY = """
query getAccounts($code: IntFilter = {}) {
  accounts(filter: {code: $code}) {
    data {
      code
      id
      name
    }
  }
}
"""

GET_DIMENSIONABLES_QUERY = """
query GetDimensionables($type: DimensionableType!) {
  dimensionables(type: $type) {
    id
    name
  }
}
"""

GET_PROPERTIES_QUERY = """
query GetProperties($filter: [PropertyFilter!] = {}) {
  properties(filter: $filter) {
    data {
      id
      name
    }
  }
}
"""

#TODO verificer denne
GET_LEASEAGREEMENTS_QUERY = """
query GetLeaseAgreements {
  leaseAgreements {
    id
    tenantName
    property {
      id
      name
    }
  }
}
"""


GET_LEASES_QUERY = """
query GetLeases($filter: [LeaseFilter!] = {_any: {}}) {
  leases(filter: $filter) {
    data {
      id
      fullAddress
      street
      streetNumber
      floor
      door
      zip
      city
      partialAddress
    }
  }
}
"""